"""QQ 官方机器人 API v2 常量(精简自 hermes qqbot 参考实现的按需子集)"""

# API 端点
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

# 连接参数
CONNECT_TIMEOUT_SECONDS = 20.0
DEFAULT_API_TIMEOUT = 30.0
RATE_LIMIT_DELAY = 60
QUICK_DISCONNECT_THRESHOLD = 5.0
MAX_QUICK_DISCONNECT_COUNT = 3
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100

# 消息限制
MAX_MESSAGE_LENGTH = 4000
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000

# Intent 位(仅 GROUP_AND_C2C_EVENT；群@收到即忽略)
INTENTS = 1 << 25

# 消息类型(发送 body 用)
MSG_TYPE_TEXT = 0
