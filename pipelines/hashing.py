"""Hash de versao do conteudo bruto de um arquivo, para detectar mudanca entre ingestoes.

Nao precisa ser inteligivel: e so um codigo que muda quando o conteudo baixado muda.
Aplicado ao conteudo ANTES do parse (texto extraido do HTML, bytes do PDF, CSV da planilha),
para decidir se vale reparsear/reingerir sem disparar o LLM a toa.
"""
import hashlib

def source_hash(raw):
    # aceita str (HTML/CSV) ou bytes (PDF); mesmo conteudo -> mesmo codigo
    b = raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode("utf-8")
    return hashlib.sha256(b).hexdigest()[:16]
