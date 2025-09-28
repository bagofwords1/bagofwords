
### Local Development

#### Prerequisites
- Python 3.12+
- Node.js 18+
- Yarn

#### Backend Setup
```bash
# Setup Python environment
cd backend
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements_versioned.txt

# Run migrations
alembic upgrade head

# Start server
python main.py  # Available at http://localhost:8000
```

#### Frontend Setup
```bash
cd frontend
yarn install
yarn dev      # Regular mode
```

- OpenAPI docs: http://localhost:8000/docs

## Links

- Website: https://bagofwords.com
- Docs: https://docs.bagofwords.com

## License
AGPL-3.0