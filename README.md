"""网页请求录制 / 查看 / 回放工具（类似浏览器开发者工具的 Network 面板）

核心思路：
  1. record    用 Playwright 驱动真实浏览器，把页面发出的请求（尤其是表单 POST）
               连同请求头、表单字段、Cookie、响应内容一起存成 JSON；
  2. list/show 像 Network 面板一样查看、过滤、导出 cURL / Python 代码；
  3. replay    用 requests 按录制顺序重放，支持变量替换、Cookie 复用、结果保存；
  4. from-curl 如果你已经会按 F12 复制请求，可以直接把 "Copy as cURL" 粘进来；
  5. report    生成一个可搜索、可展开的 HTML 报告（离线可看）。

常用示例
--------
# 本文件位于 C:/code/tools/，在其它目录执行时请写完整路径，例如：
#   python "C:/code/tools/web_request_recorder.py" --help

# 录制：打开浏览器，手动登录/提交表单，回到控制台按回车结束
python web_request_recorder.py record --url [https://example.com/login](https://example.com/login)


# 只想看“点了按钮后提交了什么”：只保留带字段的请求，并实时打印字段值
python web_request_recorder.py record --url [https://example.com/login](https://example.com/login) --only-fields
python web_request_recorder.py record --url [http://127.0.0.1:5000/login](http://127.0.0.1:5000/login) --only-fields

# 只录写操作（POST/PUT/PATCH/DELETE）
python web_request_recorder.py record --url [https://example.com](https://example.com) --forms-only

# 附加到已经开着的 Chrome（先用 chrome.exe --remote-debugging-port=9222 启动）
python web_request_recorder.py record --cdp [http://127.0.0.1:9222](http://127.0.0.1:9222)

# 查看
python web_request_recorder.py list captures/rec-20261027-101500.json
python web_request_recorder.py forms captures/rec-20261027-101500.json
python web_request_recorder.py forms captures/rec-20261027-101500.json --csv forms.csv
python web_request_recorder.py show captures/rec-20261027-101500.json 3 --format curl

# 回放：替换动态字段（同名字段自动替换，正文里的 {{token}} 也会替换）
python web_request_recorder.py replay captures/rec-20261027-101500.json \
        --var username=admin --var password=123456 --save output/replay

# 从 devtools 复制的 cURL 生成可回放的录制文件
python web_request_recorder.py from-curl curl.txt -o captures/login.json

# 生成 HTML 报告
python web_request_recorder.py report captures/rec-20261027-101500.json --open

依赖：
  pip install playwright requests      # 录制需要 playwright
  playwright install chromium          # 机器上没有 Chrome/Edge 时执行一次
"""  写一篇公众号文章