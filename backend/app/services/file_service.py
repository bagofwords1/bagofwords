from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.schemas.file_schema import FileSchema, FileSchemaWithMetadata
from app.models.file import File
import uuid
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.ai.agents.excel import ExcelAgent
from app.models.sheet_schema import SheetSchema
from typing import Optional
from fastapi import HTTPException
from app.models.file import report_file_association
from openpyxl import load_workbook
from app.ai.agents.doc.doc import DocAgent
from app.models.file_tag import FileTag
import tiktoken
from sqlalchemy.ext.asyncio import AsyncSession
import aiofiles  # Add this import for async file operations
from sqlalchemy import select, exists
from app.models.llm_model import LLMModel

class FileService:
    def __init__(self):
        pass

    async def upload_file(self, db: AsyncSession, file: UploadFile, current_user: User, organization: Organization, report_id: Optional[str] = None) -> FileSchema:
        # Generate a unique filename to prevent overwriting existing files
        unique_filename = f"{uuid.uuid4()}_{file.filename}"
        file_location = f"uploads/{unique_filename}"

        # Async file writing
        async with aiofiles.open(file_location, "wb") as buffer:
            content = await file.read()
            await buffer.write(content)

        # Create the database entry
        db_file = File(
            filename=file.filename,
            content_type=file.content_type,
            path=file_location,
            user_id = current_user.id,
            organization_id = organization.id)

        db.add(db_file)
        await db.commit()
        await db.refresh(db_file)
        if report_id:
            stmt = select(Report).filter(Report.id == report_id)
            result = await db.execute(stmt)
            report = result.scalar_one_or_none()
            
            if report:
                report.files.append(db_file)
                await db.commit()
                await db.refresh(report)

        # should be in as a seperate job 
        # currently doing this for one sheet only
        model = await organization.get_default_llm_model(db)
        if db_file.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or db_file.content_type == "application/vnd.ms-excel":
            sc = await self._create_sheet_schemas(db, db_file, model)
        elif db_file.content_type == "application/pdf":
            tags = await self._process_pdf(db, db_file, model)
        
        # Return the file schema
        file_schema = FileSchema.from_orm(db_file)
        
        return file_schema
    
    async def remove_file_from_report(self, db: AsyncSession, file_id: str, report_id: str, organization: Organization, current_user: User):
        stmt = select(Report).filter(Report.id == report_id)
        result = await db.execute(stmt)
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        stmt = select(File).filter(File.id == file_id)
        result = await db.execute(stmt)
        file = result.scalar_one_or_none()
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        stmt = select(report_file_association).filter_by(
            report_id=report_id, file_id=file_id
        )
        result = await db.execute(stmt)
        association = result.first()
        if not association:
            raise HTTPException(status_code=404, detail="File is not associated with this report")

        await db.execute(
            report_file_association.delete().where(
                (report_file_association.c.report_id == report_id) &
                (report_file_association.c.file_id == file_id)
            )
        )
        await db.commit()

        return True
        
    async def get_files(self, db: AsyncSession, organization: Organization):
        stmt = select(File).filter(File.organization_id == organization.id)
        result = await db.execute(stmt)
        files = result.scalars().all()

        # get files with tags
        for file in files:
            stmt = select(FileTag).filter(FileTag.file_id == file.id)
            result = await db.execute(stmt)
            file.tags = result.scalars().all()

            stmt = select(SheetSchema).filter(SheetSchema.file_id == file.id)
            result = await db.execute(stmt)
            file.schemas = result.scalars().all()

        return files

    async def get_files_by_report(self, db: AsyncSession, report_id: str, organization: Organization):
        stmt = select(Report).filter(Report.id == report_id)
        result = await db.execute(stmt)
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
            
        files = report.files
        return [FileSchema.from_orm(file) for file in files]

    async def _create_sheet_schemas(self, db: AsyncSession, file: File, model: LLMModel):
        if file.content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" or file.content_type == "application/vnd.ms-excel":
            # Note: load_workbook is synchronous, consider running in threadpool for production
            workbook = load_workbook(filename=file.path, read_only=True)
            try:
                sheet_names = workbook.sheetnames
                
                for index, sheet_name in enumerate(sheet_names):
                    ea = ExcelAgent(file, model)
                    schema = ea.get_schema(index)

                    sc = SheetSchema(
                        sheet_name=schema["sheet_name"],
                        sheet_index=index,
                        schema=schema,
                        file_id=file.id
                    )
                    db.add(sc)
                
                await db.commit()
                return len(sheet_names)
            finally:
                workbook.close()

    async def _process_pdf(self, db: AsyncSession, file: File, model: LLMModel):
        da = DocAgent(file, model)
        content = da.get_content()

        tags = []   
        tokenizer = tiktoken.encoding_for_model("gpt-4o")

        tokens = tokenizer.encode(content)
        chunk_size = 100000
        overlap = 300

        # for chunk_of_9k, extract tags from text
        for i in range(0, len(tokens), chunk_size - overlap):
            chunk = tokenizer.decode(tokens[i:i+chunk_size])
            new_tags = da.get_tags_from_text(chunk, tags)
            tags.extend(new_tags)
        
        # Create a list to hold all FileTag objects
        file_tags = []
        
        for tag in tags:
            # Assuming FileTag is a model with fields 'tag', 'value', and 'file_id'
            file_tag = FileTag(
                key=tag["tag"],
                value=tag["value"],
                file_id=file.id
            )
            file_tags.append(file_tag)
        
        # Replace bulk_save_objects with add_all
        for file_tag in file_tags:
            db.add(file_tag)
        await db.commit()
        
        return tags


    async def create_or_get_report_file_association(self, db: AsyncSession, report_id: str, file_id: str):
        # 1. Fetch Report and File
        report_stmt = select(Report).where(Report.id == report_id)
        report_result = await db.execute(report_stmt)
        report = report_result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail=f"Report not found: {report_id}")

        file_stmt = select(File).where(File.id == file_id)
        file_result = await db.execute(file_stmt)
        file = file_result.scalar_one_or_none()
        if not file:
            raise HTTPException(status_code=404, detail=f"File not found: {file_id}")

        # 2. Check if association already exists (more efficient check)
        # Assuming 'files' is the relationship attribute on the Report model
        # Adjust if the relationship is defined differently
        association_exists_stmt = select(exists().where(
            report_file_association.c.report_id == report_id,
            report_file_association.c.file_id == file_id
        ))
        association_exists = await db.scalar(association_exists_stmt)

        # 3. If not associated, create the association by appending
        if not association_exists:
            try:
                # Append the file to the report's collection. SQLAlchemy handles the insert.
                # Ensure the relationship is correctly defined in your models
                # (e.g., on Report: files = relationship("File", secondary=report_file_association, backref="reports"))
                # If the relationship is defined on the File model instead (e.g., file.reports.append(report)), use that.
                report.files.append(file) 
                db.add(report) # Add the modified report to the session if needed
                await db.commit()
                await db.refresh(report) # Refresh report to potentially load the updated relationship
                print(f"Association created between Report {report_id} and File {file_id}")
                return True # Indicate association was created
            except Exception as e:
                await db.rollback()
                print(f"Error creating association: {e}") # Log the specific error
                # Consider raising a specific exception or HTTPException
                raise HTTPException(status_code=500, detail=f"Failed to create association: {e}")
        else:
            print(f"Association already exists between Report {report_id} and File {file_id}")
            return False # Indicate association already existed
        