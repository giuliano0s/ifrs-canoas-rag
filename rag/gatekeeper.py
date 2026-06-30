import os
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_ratelimit import Ratelimit, SlidingWindow

load_dotenv()

# cliente redis e limitador de 20 requisições por minuto por identificador
redis = Redis(
    url=os.getenv("UPSTASH_REDIS_ENDPOINT"),
    token=os.getenv("UPSTASH_REDIS_API_KEY")
)

ratelimit = Ratelimit(
    redis=redis,
    limiter=SlidingWindow(max_requests=20, window=60),
    prefix="ifrs-canoas-chat"
)


def client_ip(request):
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "desconhecido"


def check_rate_limit(request):
    identifier = client_ip(request)
    result = ratelimit.limit(identifier)
    return result.allowed, result.reset
