import os
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

# 模擬後端邏輯
app = FastAPI()
templates = Jinja2Templates(directory="templates")
LIFF_ID = "test-id"

try:
    # 模擬渲染過程
    print("嘗試手動渲染模板...")
    test_context = {"request": {}, "liff_id": LIFF_ID}
    # 這裡只測試模板是否能找到
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader('templates'))
    template = env.get_template('liff_form.html')
    output = template.render(liff_id=LIFF_ID)
    print("渲染成功！")
except Exception as e:
    print(f"渲染失敗！錯誤：{e}")
