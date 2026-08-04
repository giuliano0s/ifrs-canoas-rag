import os
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_ratelimit import Ratelimit, SlidingWindow

load_dotenv()

redis = Redis(
    url=os.getenv("UPSTASH_REDIS_ENDPOINT"),
    token=os.getenv("UPSTASH_REDIS_API_KEY")
)

# DOIS limitadores, porque o wifi do campus faz NAT: dezenas de alunos compartilham um IP, e um
# teto por IP sozinho bloquearia a turma inteira junto (numa aula/demonstracao, o 1o aluno consome
# a cota de todos). O limite normal e por DISPOSITIVO (user_id anonimo do localStorage, que o
# widget ja envia); o IP fica como teto secundario, folgado, contra abuso de um unico host.
ratelimit = Ratelimit(
    redis=redis,
    limiter=SlidingWindow(max_requests=20, window=60),
    prefix="ifrs-canoas-chat"
)

ratelimit_ip = Ratelimit(
    redis=redis,
    limiter=SlidingWindow(max_requests=120, window=60),
    prefix="ifrs-canoas-chat-ip"
)


# teto global de requisicoes por dia (proxy simples de teto de gasto no piloto); override por env.
# 6000 (nao 2000) para o teste publico: 2000 era atingivel num dia de divulgacao e a recusa
# ("limite de uso de hoje") atinge todos os usuarios de uma vez.
GLOBAL_DAILY_MAX = int(os.getenv("GLOBAL_DAILY_MAX", "6000"))


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


def check_rate_limit(request, user_id=None):
    # limite por DISPOSITIVO quando o widget manda o user_id (evita punir a turma que compartilha o
    # IP do wifi); sem user_id (chamada direta a API), cai no IP com o mesmo teto estrito.
    # O teto por IP roda SEMPRE, folgado, para um host abusivo nao escapar trocando de user_id.
    ip = client_ip(request)
    por_ip = ratelimit_ip.limit(ip)
    if not por_ip.allowed:
        return False, por_ip.reset
    result = ratelimit.limit(user_id or ip)
    return result.allowed, result.reset


def check_global_budget():
    # circuit breaker de custo: conta as requisicoes do dia (UTC) numa chave que expira sozinha.
    # cada request ja tem custo limitado pelos caps de tamanho; isto poe um teto no volume total.
    # fail-closed: se o Redis falhar, NEGA (nao serve sem a protecao) em vez de liberar sem teto.
    from datetime import datetime, timezone
    dia = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    chave = f"ifrs-canoas-chat:global:{dia}"
    try:
        n = redis.incr(chave)
        if n == 1:
            redis.expire(chave, 90000)
        return n <= GLOBAL_DAILY_MAX
    except Exception:
        return False
