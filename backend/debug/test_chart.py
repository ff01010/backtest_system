import json
from playwright.sync_api import sync_playwright

URL = "http://localhost:5175/?symbol=688981.SH&date=20260624&modules=A,B&sides=BUY,SELL"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(3000)

    # 选 tick 信号文件
    selects = page.locator('select')
    count = selects.count()
    print(f"下拉框数量: {count}")
    for i in range(count):
        text = selects.nth(i).inner_text()
        print(f"  [{i}] {text[:80]}")

    # 信号文件下拉框是最后一个 select
    signal_select = selects.last()
    signal_select.select_option(value="tick/688981_SH_tick_20260624.csv")
    page.wait_for_timeout(3000)

    # 截图
    page.screenshot(path="/tmp/chart_tick.png", full_page=False)
    print("截图: /tmp/chart_tick.png")

    # 看页面上是否有 marker 相关内容
    page_content = page.content()
    # 检查 canvas (lightweight-charts 用 canvas 渲染)
    canvas = page.locator('canvas')
    print(f"Canvas 数量: {canvas.count()}")

    # 检查 console 错误
    errors = []
    page.on("console", lambda msg: None)  # Ignore regular logs
    page.on("pageerror", lambda err: errors.append(str(err)))
    if errors:
        print(f"页面错误: {errors[:5]}")

    browser.close()
