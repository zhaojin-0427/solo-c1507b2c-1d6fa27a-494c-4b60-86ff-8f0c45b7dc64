"""本机启动入口：python3 run.py 后访问 http://127.0.0.1:8000/docs"""
import uvicorn

from app.main import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
