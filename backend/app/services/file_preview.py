"""
File Preview Service

Generates raw previews for uploaded files (CSV, Excel, PDF) without LLM.
Used to provide context for the coder to generate appropriate data extraction code.
"""

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Content type constants
EXCEL_XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXCEL_XLS_TYPE = "application/vnd.ms-excel"
EXCEL_TYPES = [EXCEL_XLSX_TYPE, EXCEL_XLS_TYPE]

PDF_TYPE = "application/pdf"
PDF_TYPES = [PDF_TYPE]

CSV_TYPES = ["text/csv", "application/csv", "text/plain"]

# Preview limits
MAX_PREVIEW_ROWS = 50
MAX_PREVIEW_SHEETS = 5
MAX_PREVIEW_COLS = 50
MAX_PDF_PAGES = 5
MAX_PDF_TEXT_CHARS = 5000
MAX_PDF_PAGE_CHARS = 2000


def generate_file_preview(file) -> Dict[str, Any]:
    """
    Generate a raw preview for a file (no LLM).
    
    Args:
        file: File model instance with path and content_type attributes
        
    Returns:
        Dict containing preview data based on file type
    """
    content_type = file.content_type
    path = file.path
    filename = file.filename
    
    try:
        if content_type in EXCEL_TYPES:
            return _preview_excel(path, filename)
        elif content_type in PDF_TYPES:
            return _preview_pdf(path, filename)
        elif content_type in CSV_TYPES or filename.lower().endswith('.csv'):
            return _preview_csv(path, filename)
        else:
            return {
                "type": "unsupported",
                "filename": filename,
                "content_type": content_type,
                "message": "Preview not available for this file type"
            }
    except Exception as e:
        logger.error(f"Error generating preview for {filename}: {e}")
        return {
            "type": "error",
            "filename": filename,
            "content_type": content_type,
            "error": str(e)
        }


def _preview_excel(path: str, filename: str) -> Dict[str, Any]:
    """Generate raw preview for Excel files (.xlsx, .xls)."""
    try:
        xl = pd.ExcelFile(path)
        sheet_names = xl.sheet_names
        
        preview = {
            "type": "excel",
            "filename": filename,
            "sheets": sheet_names,
            "sheet_count": len(sheet_names),
            "sheet_previews": {}
        }
        
        # Preview first N sheets
        for sheet in sheet_names[:MAX_PREVIEW_SHEETS]:
            try:
                # Read without header to get raw cell values
                df = pd.read_excel(path, sheet_name=sheet, header=None, nrows=MAX_PREVIEW_ROWS)
                
                # Limit columns
                if len(df.columns) > MAX_PREVIEW_COLS:
                    df = df.iloc[:, :MAX_PREVIEW_COLS]
                
                # Get total shape by reading just first row of full data
                try:
                    df_shape = pd.read_excel(path, sheet_name=sheet, header=None)
                    total_rows = len(df_shape)
                    total_cols = len(df_shape.columns)
                except Exception:
                    total_rows = len(df)
                    total_cols = len(df.columns)
                
                # Convert to raw cell values (handle NaN, dates, etc.)
                raw_cells = _dataframe_to_raw_cells(df)
                
                preview["sheet_previews"][sheet] = {
                    "raw_cells": raw_cells,
                    "shape": [total_rows, total_cols],
                    "preview_rows": len(raw_cells),
                    "preview_cols": len(raw_cells[0]) if raw_cells else 0
                }
            except Exception as e:
                logger.warning(f"Error previewing sheet '{sheet}' in {filename}: {e}")
                preview["sheet_previews"][sheet] = {
                    "error": str(e),
                    "raw_cells": [],
                    "shape": [0, 0]
                }
        
        xl.close()
        return preview
        
    except Exception as e:
        logger.error(f"Error opening Excel file {filename}: {e}")
        return {
            "type": "excel",
            "filename": filename,
            "error": str(e),
            "sheets": [],
            "sheet_previews": {}
        }


def _preview_csv(path: str, filename: str) -> Dict[str, Any]:
    """Generate preview for CSV files."""
    try:
        # First, try to detect encoding and read
        df = pd.read_csv(path, nrows=MAX_PREVIEW_ROWS)
        
        # Limit columns
        if len(df.columns) > MAX_PREVIEW_COLS:
            df = df.iloc[:, :MAX_PREVIEW_COLS]
        
        # Get total row count (approximate for large files)
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                total_rows = sum(1 for _ in f) - 1  # Subtract header
        except Exception:
            total_rows = len(df)
        
        # Convert to records for preview
        head_records = df.head(MAX_PREVIEW_ROWS).fillna("").to_dict(orient="records")
        
        return {
            "type": "csv",
            "filename": filename,
            "columns": list(df.columns),
            "dtypes": {str(k): str(v) for k, v in df.dtypes.items()},
            "shape": [total_rows, len(df.columns)],
            "head": head_records,
            "preview_rows": len(head_records)
        }
        
    except Exception as e:
        logger.error(f"Error reading CSV file {filename}: {e}")
        return {
            "type": "csv",
            "filename": filename,
            "error": str(e),
            "columns": [],
            "head": []
        }


def _preview_pdf(path: str, filename: str) -> Dict[str, Any]:
    """Generate preview for PDF files."""
    try:
        from pypdf import PdfReader
        
        reader = PdfReader(path)
        total_pages = len(reader.pages)
        
        pages_preview = []
        text_parts = []
        
        # Extract text from first N pages
        for i, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
            try:
                text = page.extract_text() or ""
                # Truncate page text
                truncated_text = text[:MAX_PDF_PAGE_CHARS]
                pages_preview.append({
                    "page": i + 1,
                    "text": truncated_text,
                    "truncated": len(text) > MAX_PDF_PAGE_CHARS
                })
                text_parts.append(text)
            except Exception as e:
                logger.warning(f"Error extracting text from page {i+1} of {filename}: {e}")
                pages_preview.append({
                    "page": i + 1,
                    "text": "",
                    "error": str(e)
                })
        
        # Combined text preview
        combined_text = "\n\n".join(text_parts)
        text_preview = combined_text[:MAX_PDF_TEXT_CHARS]
        
        return {
            "type": "pdf",
            "filename": filename,
            "pages": total_pages,
            "text_preview": text_preview,
            "text_truncated": len(combined_text) > MAX_PDF_TEXT_CHARS,
            "pages_preview": pages_preview,
            "preview_pages": len(pages_preview)
        }
        
    except ImportError:
        logger.error("pypdf not installed, cannot preview PDF")
        return {
            "type": "pdf",
            "filename": filename,
            "error": "PDF library not available",
            "pages": 0,
            "text_preview": ""
        }
    except Exception as e:
        logger.error(f"Error reading PDF file {filename}: {e}")
        return {
            "type": "pdf",
            "filename": filename,
            "error": str(e),
            "pages": 0,
            "text_preview": ""
        }


def _dataframe_to_raw_cells(df: pd.DataFrame) -> List[List[Any]]:
    """
    Convert DataFrame to raw cell values, handling special types.
    
    Returns list of lists representing rows of cells.
    """
    rows = []
    for _, row in df.iterrows():
        cells = []
        for val in row:
            cells.append(_convert_cell_value(val))
        rows.append(cells)
    return rows


def _convert_cell_value(val: Any) -> Any:
    """Convert a cell value to a JSON-serializable format."""
    if pd.isna(val):
        return None
    elif isinstance(val, (pd.Timestamp, pd.DatetimeTZDtype)):
        return val.isoformat() if hasattr(val, 'isoformat') else str(val)
    elif isinstance(val, (int, float, str, bool)):
        # Handle infinity and nan for floats
        if isinstance(val, float):
            if pd.isna(val) or val != val:  # NaN check
                return None
            if val == float('inf') or val == float('-inf'):
                return str(val)
        return val
    else:
        # Convert to string for unknown types
        return str(val)


def render_file_description(preview: Dict[str, Any], path: str) -> str:
    """
    Render a file preview as a human-readable description for coder context.
    
    Args:
        preview: Preview dict from generate_file_preview()
        path: File path
        
    Returns:
        Formatted string description
    """
    if not preview:
        return f"File at {path} (no preview available)"
    
    file_type = preview.get("type", "unknown")
    filename = preview.get("filename", "unknown")
    
    if preview.get("error"):
        return f"File: {filename} at {path}\nError: {preview['error']}"
    
    if file_type == "excel":
        return _render_excel_description(preview, path)
    elif file_type == "csv":
        return _render_csv_description(preview, path)
    elif file_type == "pdf":
        return _render_pdf_description(preview, path)
    elif file_type == "unsupported":
        return f"File: {filename} at {path}\nType: {preview.get('content_type', 'unknown')}\n{preview.get('message', '')}"
    
    return f"File: {filename} at {path}"


def _render_excel_description(preview: Dict[str, Any], path: str) -> str:
    """Render Excel preview as description."""
    lines = [
        f"Excel File: {preview.get('filename', 'unknown')}",
        f"Path: {path}",
        f"Sheets: {preview.get('sheets', [])}",
        f"Total sheets: {preview.get('sheet_count', 0)}",
        ""
    ]
    
    sheet_previews = preview.get("sheet_previews", {})
    for sheet_name, sheet_data in list(sheet_previews.items())[:3]:  # Show first 3 sheets
        shape = sheet_data.get("shape", [0, 0])
        lines.append(f"=== Sheet: {sheet_name} ({shape[0]} rows x {shape[1]} cols) ===")
        
        if sheet_data.get("error"):
            lines.append(f"Error: {sheet_data['error']}")
        else:
            raw_cells = sheet_data.get("raw_cells", [])
            # Show first 20 rows
            for i, row in enumerate(raw_cells[:20]):
                # Format row, truncating long values
                formatted_row = [_truncate_cell(cell) for cell in row]
                lines.append(f"Row {i}: {formatted_row}")
            
            if len(raw_cells) > 20:
                lines.append(f"... ({len(raw_cells) - 20} more preview rows)")
        
        lines.append("")
    
    if len(sheet_previews) > 3:
        lines.append(f"... and {len(sheet_previews) - 3} more sheets")
    
    return "\n".join(lines)


def _render_csv_description(preview: Dict[str, Any], path: str) -> str:
    """Render CSV preview as description."""
    shape = preview.get("shape", [0, 0])
    lines = [
        f"CSV File: {preview.get('filename', 'unknown')}",
        f"Path: {path}",
        f"Shape: {shape[0]} rows x {shape[1]} columns",
        f"Columns: {preview.get('columns', [])}",
        f"Data types: {preview.get('dtypes', {})}",
        "",
        "Sample rows:"
    ]
    
    head = preview.get("head", [])
    for i, row in enumerate(head[:15]):
        lines.append(f"Row {i}: {row}")
    
    if len(head) > 15:
        lines.append(f"... ({len(head) - 15} more preview rows)")
    
    return "\n".join(lines)


def _render_pdf_description(preview: Dict[str, Any], path: str) -> str:
    """Render PDF preview as description."""
    lines = [
        f"PDF File: {preview.get('filename', 'unknown')}",
        f"Path: {path}",
        f"Total pages: {preview.get('pages', 0)}",
        f"Preview pages: {preview.get('preview_pages', 0)}",
        "",
        "Text preview:",
        preview.get("text_preview", "(no text extracted)"),
    ]
    
    if preview.get("text_truncated"):
        lines.append("... (text truncated)")
    
    return "\n".join(lines)


def _truncate_cell(val: Any, max_len: int = 50) -> Any:
    """Truncate a cell value for display."""
    if val is None:
        return None
    if isinstance(val, str) and len(val) > max_len:
        return val[:max_len] + "..."
    return val

