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


# teto global de requisicoes por dia (proxy simples de teto de gasto no piloto); override por env
GLOBAL_DAILY_MAX = int(os.getenv("GLOBAL_DAILY_MAX", "5000"))


def client_ip(request):
    # IP resistente a spoof atras do Vercel: o cliente controla o X-Forwarded-For que ELE manda,
    # entao o 1o item da lista e falsificavel. Preferimos o X-Real-IP (setado pela borda do Vercel)
    # e, na falta, o ULTIMO item do X-Forwarded-For (o que o proxy confiavel acrescentou), nunca o 1o.
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.remote_addr or "desconhecido"


def check_rate_limit(request):
    identifier = client_ip(request)
    result = ratelimit.limit(identifier)
    return result.allowed, result.reset


def check_global_budget():
    # circuit breaker de custo: conta as requisicoes do dia (UTC) numa chave que expira sozinha.
    # cada request ja tem custo limitado pelos caps de tamanho; isto poe um teto no volume total.
    # fail-open: se o Redis falhar, deixa passar em vez de derrubar o servico.
    from datetime import datetime, timezone
    dia = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chave = f"ifrs-canoas-chat:global:{dia}"
    try:
        n = redis.incr(chave)
        if n == 1:
            redis.expire(chave, 90000)
        return n <= GLOBAL_DAILY_MAX
    except Exception:
        return True
