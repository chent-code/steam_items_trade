"""运行时路径基准(全项目唯一来源)

目录约定:
    <repo>/core/    本模块 + 全部公共 Python 模块
    <repo>/config/  config.example.json(入库模板)
    <repo>/data/    运行时数据(gitignore):config.json、cookie、state、logs/、reports/

core 下的模块一律 `from paths import ...` 取路径,不要用 __file__ 拼相对路径。
"""
import os

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CORE_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")
REPORT_DIR = os.path.join(DATA_DIR, "reports")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")  # 真实配置(含密钥,gitignore)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)
