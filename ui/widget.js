(function () {
  // endpoint relativo, mesma origem do front no vercel
  const CHAT_URL = "/chat";

  // injeta estilos
  const style = document.createElement("style");
  style.textContent = `
    #ifrs-chat-btn {
      position: fixed;
      bottom: 28px;
      right: 28px;
      width: 60px;
      height: 60px;
      border-radius: 50%;
      background: #007B3A;
      color: white;
      border: none;
      cursor: pointer;
      box-shadow: 0 4px 16px rgba(0,0,0,0.25);
      font-size: 28px;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 9999;
      transition: transform 0.2s, box-shadow 0.2s;
    }

    #ifrs-chat-btn:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 24px rgba(0,0,0,0.3);
    }

    #ifrs-chat-window {
      position: fixed;
      bottom: 100px;
      right: 28px;
      width: min(370px, calc(100vw - 32px));
      height: min(520px, calc(100vh - 130px));
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.18);
      display: none;
      flex-direction: column;
      z-index: 9998;
      overflow: hidden;
      font-family: 'Segoe UI', sans-serif;
    }

    #ifrs-chat-window.open {
      display: flex;
      animation: slideUp 0.25s ease;
    }

    @keyframes slideUp {
      from { opacity: 0; transform: translateY(16px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    #ifrs-chat-header {
      background: #007B3A;
      color: white;
      padding: 14px 18px;
      font-weight: 600;
      font-size: 15px;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    #ifrs-chat-header span {
      font-size: 20px;
    }

    #ifrs-chat-messages {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      background: #f7f7f7;
    }

    .ifrs-msg {
      max-width: 85%;
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 14px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .ifrs-msg.user {
      align-self: flex-end;
      background: #007B3A;
      color: white;
      border-bottom-right-radius: 4px;
    }

    .ifrs-msg.bot {
      align-self: flex-start;
      background: white;
      color: #222;
      border: 1px solid #e0e0e0;
      border-bottom-left-radius: 4px;
    }

    .ifrs-msg.typing {
      color: #999;
      font-style: italic;
      background: white;
      border: 1px solid #e0e0e0;
    }

    #ifrs-chat-input-area {
      display: flex;
      padding: 12px;
      border-top: 1px solid #eee;
      background: white;
      gap: 8px;
    }

    #ifrs-chat-input {
      flex: 1;
      border: 1px solid #ddd;
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 14px;
      outline: none;
      resize: none;
    }

    #ifrs-chat-input:focus {
      border-color: #007B3A;
    }

    #ifrs-chat-send {
      background: #007B3A;
      color: white;
      border: none;
      border-radius: 8px;
      padding: 8px 14px;
      cursor: pointer;
      font-size: 18px;
      transition: background 0.2s;
    }

    #ifrs-chat-send:hover {
      background: #005c2c;
    }

    #ifrs-chat-send:disabled {
      background: #aaa;
      cursor: not-allowed;
    }
  `;
  document.head.appendChild(style);

  // botão flutuante
  const btn = document.createElement("button");
  btn.id = "ifrs-chat-btn";
  btn.innerHTML = "💬";
  btn.title = "Assistente IFRS";
  document.body.appendChild(btn);

  // janela de chat
  const win = document.createElement("div");
  win.id = "ifrs-chat-window";
  win.innerHTML = `
    <div id="ifrs-chat-header">
      <span>🎓</span> Assistente IFRS Canoas
    </div>
    <div id="ifrs-chat-messages">
      <div class="ifrs-msg bot">Olá! Sou o assistente virtual do IFRS Campus Canoas. Como posso te ajudar?</div>
    </div>
    <div id="ifrs-chat-input-area">
      <textarea id="ifrs-chat-input" rows="1" placeholder="Digite sua pergunta..."></textarea>
      <button id="ifrs-chat-send">➤</button>
    </div>
  `;
  document.body.appendChild(win);

  // abre e fecha
  btn.addEventListener("click", () => {
    win.classList.toggle("open");
  });

  const messages = win.querySelector("#ifrs-chat-messages");
  const input = win.querySelector("#ifrs-chat-input");
  const sendBtn = win.querySelector("#ifrs-chat-send");

  // historico apenas em memoria, comeca vazio a cada carregamento
  let history = [];

  // identidade anonima para telemetria: userId opaco por dispositivo (persiste no localStorage,
  // sobrevive ao F5), e sessionId por carregamento de pagina (mesma vida do historico, zera no reload)
  let userId = null;
  try {
    userId = localStorage.getItem("ifrs_chat_uid");
    if (!userId) {
      userId = (window.crypto && crypto.randomUUID)
        ? crypto.randomUUID()
        : String(Date.now()) + Math.random().toString(16).slice(2);
      localStorage.setItem("ifrs_chat_uid", userId);
    }
  } catch (e) {
    userId = null; // localStorage indisponivel (ex: modo restrito): segue sem identificar
  }
  const sessionId = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : String(Date.now()) + Math.random().toString(16).slice(2);

  // transforma URLs do texto em links clicaveis, preservando o resto como texto
  function linkify(text) {
    const frag = document.createDocumentFragment();
    const urlRegex = /(https?:\/\/[^\s)\]]+)/g;
    let last = 0;
    let match;
    while ((match = urlRegex.exec(text)) !== null) {
      if (match.index > last) {
        frag.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      const a = document.createElement("a");
      a.href = match[0];
      a.textContent = match[0];
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.style.color = "#007B3A";
      a.style.wordBreak = "break-all";
      frag.appendChild(a);
      last = urlRegex.lastIndex;
    }
    if (last < text.length) {
      frag.appendChild(document.createTextNode(text.slice(last)));
    }
    return frag;
  }

  function addMessage(text, role) {
    const div = document.createElement("div");
    div.className = `ifrs-msg ${role}`;

    if (role === "bot" && text.includes("Fontes:")) {
        const parts = text.split("Fontes:");
        const mainText = document.createElement("span");
        mainText.textContent = parts[0].trim();

        const details = document.createElement("details");
        details.style.marginTop = "8px";
        details.style.fontSize = "12px";
        details.style.color = "#666";

        const summary = document.createElement("summary");
        summary.textContent = "Ver fontes";
        summary.style.cursor = "pointer";
        summary.style.color = "#007B3A";

        const fontes = document.createElement("div");
        fontes.style.marginTop = "6px";
        fontes.style.lineHeight = "1.8";
        fontes.appendChild(linkify("Fontes:" + parts[1]));

        details.appendChild(summary);
        details.appendChild(fontes);
        div.appendChild(mainText);
        div.appendChild(details);
    } else {
        div.textContent = text;
    }

    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  async function send() {
    const query = input.value.trim();
    if (!query) return;

    input.value = "";
    sendBtn.disabled = true;
    addMessage(query, "user");

    const typing = addMessage("Digitando...", "bot typing");

    try {
      const res = await fetch(CHAT_URL, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query, history, user_id: userId, session_id: sessionId })
      });
      typing.remove();

      // resposta do rate limiter
      if (res.status === 429) {
        addMessage("Muitas perguntas em pouco tempo. Aguarde um instante e tente novamente.", "bot");
        sendBtn.disabled = false;
        input.focus();
        return;
      }

      const data = await res.json();
      addMessage(data.response, "bot");

      // acumula contexto da conversa ativa, descartado ao recarregar
      history.push({ role: "user", content: query });
      history.push({ role: "assistant", content: data.response });
    } catch (e) {
      typing.remove();
      addMessage("Erro ao conectar com o assistente.", "bot");
    }

    sendBtn.disabled = false;
    input.focus();
  }

  sendBtn.addEventListener("click", send);

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
})();