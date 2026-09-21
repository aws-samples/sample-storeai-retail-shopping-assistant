const COGNITO_ENABLED = Boolean(
  process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID || process.env.NEXT_PUBLIC_COGNITO_WEB_CLIENT_ID,
);

export function avatarOrigin(): string {
  const { origin, protocol, hostname } = window.location;
  if (process.env.NODE_ENV === "production" &&
      (protocol !== "https:" || ["localhost", "127.0.0.1", "[::1]"].includes(hostname))) {
    throw new Error("The avatar requires the deployed HTTPS application origin.");
  }
  return origin;
}

export function avatarSenderUrl(idToken: string | null): string {
  if (COGNITO_ENABLED && !idToken) throw new Error("Sign in before using the avatar.");
  const url = new URL("/ws", avatarOrigin());
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const override = process.env.NEXT_PUBLIC_LIVEAVATAR_URL;
  if (override) {
    const configured = new URL(override);
    if (configured.origin !== url.origin || configured.pathname !== "/ws") {
      throw new Error("Avatar WebSockets must use the application's /ws endpoint.");
    }
  }
  if (idToken) url.searchParams.set("token", idToken);
  return url.href;
}

/** Own one popup and broker credentials without sending a Cognito token to it. */
export function createAvatarWindow(
  getToken: () => string | null,
  onError: (message: string) => void,
) {
  const origin = avatarOrigin();
  let popup: Window | null = null;
  let generation = 0;
  let pairing = false;
  let issuingSession = false;
  const pending = new Set<AbortController>();

  const post = (win: Window, data: object) => win.postMessage(data, origin);
  const onMessage = async (event: MessageEvent) => {
    if (event.origin !== origin || event.source !== popup || !popup || popup.closed) return;
    const data = event.data;
    if (!data || typeof data.requestId !== "string" || data.requestId.length > 80) return;
    const kind = data.type;
    if (kind !== "avatar-ready" && kind !== "avatar-token-request") return;
    if (kind === "avatar-ready" ? pairing : issuingSession) return;
    const win = popup;
    const attempt = generation;
    const token = getToken();
    if (!token) {
      post(win, { type: "avatar-auth-error", requestId: data.requestId, error: "Sign in to StoreAI." });
      return;
    }
    if (kind === "avatar-token-request" &&
        (typeof data.avatarId !== "string" || data.avatarId.length > 128 ||
         typeof data.voiceId !== "string" || data.voiceId.length > 128)) return;
    if (kind === "avatar-ready") pairing = true;
    else issuingSession = true;
    const controller = new AbortController();
    pending.add(controller);
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(new URL(kind === "avatar-ready" ? "/pair" : "/token", origin), {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: kind === "avatar-token-request"
          ? JSON.stringify({ avatar_id: data.avatarId, voice_id: data.voiceId }) : undefined,
        cache: "no-store",
        redirect: "error",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Avatar request failed (${response.status}).`);
      const result = await response.json();
      if (generation !== attempt || popup !== win || win.closed) return;
      if (kind === "avatar-ready") {
        if (typeof result.nonce !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(result.nonce)) {
          throw new Error("Invalid avatar pairing response.");
        }
        post(win, { type: "avatar-pair", requestId: data.requestId, nonce: result.nonce });
      } else {
        if (typeof result.session_token !== "string") throw new Error("Invalid avatar session response.");
        post(win, { type: "avatar-token-result", requestId: data.requestId, sessionToken: result.session_token });
      }
    } catch (error) {
      if (generation === attempt && popup === win && !win.closed) {
        const message = error instanceof Error ? error.message : "Avatar request failed.";
        onError(message);
        post(win, { type: "avatar-auth-error", requestId: data.requestId, error: message });
      }
    } finally {
      clearTimeout(timer);
      pending.delete(controller);
      if (generation === attempt) {
        if (kind === "avatar-ready") pairing = false;
        else issuingSession = false;
      }
    }
  };
  window.addEventListener("message", onMessage);
  const close = () => {
    generation++;
    for (const request of pending) request.abort();
    pending.clear();
    popup?.close();
    popup = null;
    pairing = issuingSession = false;
  };
  return {
    open() {
      close();
      onError("");
      // Open synchronously in the click handler. The message listener is already installed.
      popup = window.open(new URL("/avatar", origin), "storeai_avatar");
      if (!popup) onError("Allow popups to open the avatar.");
    },
    close,
    dispose() {
      close();
      window.removeEventListener("message", onMessage);
    },
  };
}
