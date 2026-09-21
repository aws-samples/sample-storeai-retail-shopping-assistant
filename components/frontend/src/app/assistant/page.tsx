"use client";
import { useState, useRef, useEffect, useCallback } from "react";
import { useAuth } from "@/context/AuthContext";
import { useConfig } from "@/context/ConfigContext";
import { useCognito } from "@/context/CognitoAuthContext";
import { useToast } from "@/components/Toast";
import * as api from "@/lib/api";
import type { ChatMessage } from "@/lib/types";
import { useNovaSonic } from "@/hooks/useNovaSonic";
import { wsUrl } from "@/lib/ws";
import { avatarSenderUrl } from "@/lib/avatar";
import CameraCapture from "@/components/CameraCapture";

const API = process.env.NEXT_PUBLIC_LAMBDA_API_URL || "";
const NOVA_SONIC_PORT = process.env.NEXT_PUBLIC_NOVA_SONIC_PORT || "8505";
const LIVEAVATAR_PORT = process.env.NEXT_PUBLIC_LIVEAVATAR_PORT || "8502";
const VOICE_HOST = process.env.NEXT_PUBLIC_NOVA_SONIC_HOST || "";
const WHISPER_HOST = VOICE_HOST;
const STREAM_API = VOICE_HOST ? `https://${VOICE_HOST}` : API;

interface ExtMsg extends ChatMessage { latency?: string; tryonUrl?: string; isLoading?: boolean }

function genSessionId() { return Math.random().toString(36).slice(2, 14); }
function getOrCreateSessionId() {
  if (typeof window === "undefined") return genSessionId();
  const key = "storeai_chat_sid";
  let sid = localStorage.getItem(key);
  if (!sid) { sid = genSessionId(); localStorage.setItem(key, sid); }
  return sid;
}
function resetSessionId() {
  const ns = genSessionId();
  localStorage.setItem("storeai_chat_sid", ns);
  return ns;
}

/** Parse markdown-style bold (**text** or __text__) into HTML */
function parseMarkdown(text: string): string {
  // XSS-safe: escape HTML first so any markup in model/product text renders as literal
  // text, then apply only the allowlisted bold/italic tags.
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/__(.+?)__/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/_(.+?)_/g, '<em>$1</em>');
}

export default function AssistantPage() {
  const { customer, setCustomer, refreshCounts } = useAuth();
  const { idToken } = useCognito();
  const cfg = useConfig();
  const { toast } = useToast();
  const [messages, setMessages] = useState<ExtMsg[]>([]);
  const [zoomImg, setZoomImg] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const [connected, setConnected] = useState(false);
  const [listening, setListening] = useState<false | "connecting" | true>(false);
  const [voiceChatMode, setVoiceChatMode] = useState<"chat" | "voice">("chat");
  const [assistantAudio, setAssistantAudio] = useState(false);
  const [avatarSpeaking, setAvatarSpeaking] = useState(false);
  const novaSonic = useNovaSonic();
  // Stable ref — the hook returns a fresh object each render; using it directly in
  // callback deps would churn listenOneTurn/runVoiceSession and cause /stt reconnect storms.
  const novaSonicRef = useRef(novaSonic);
  novaSonicRef.current = novaSonic;
  const [sessionId, setSessionId] = useState(() => getOrCreateSessionId());
  const [lastKey, setLastKey] = useState<string | null>(null);
  const [tryonPid, setTryonPid] = useState<string | null>(null);
  const [tryonResult, setTryonResult] = useState<string | null>(null);
  const [tryonGarment, setTryonGarment] = useState("UPPER_BODY");
  const [tryonRunning, setTryonRunning] = useState(false);
  const [sizeRec, setSizeRec] = useState<any>(null);
  const [postCheckoutTime, setPostCheckoutTime] = useState<number | null>(null);
  const [photoUploading, setPhotoUploading] = useState(false);
  const [photoUploaded, setPhotoUploaded] = useState(false);
  const [photoUrl, setPhotoUrl] = useState("");
  const [boothQueue, setBoothQueue] = useState<any[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const photoRef = useRef<HTMLInputElement>(null);
  const [photoFile, setPhotoFile] = useState<File | null>(null);
  const productCache = useRef<Record<string, any>>({});
  const [productCacheVer, setProductCacheVer] = useState(0);
  const cartSizeCache = useRef<Record<string, string>>({});  // product_id → selected size

  // Refresh cart size cache when customer changes or counts refresh
  useEffect(() => {
    if (!customer) { cartSizeCache.current = {}; return; }
    api.getCart(customer.customer_id).then((r) => {
      const map: Record<string, string> = {};
      for (const item of (r.items || [])) map[item.product_id] = item.size;
      cartSizeCache.current = map;
      setProductCacheVer((v) => v + 1);  // trigger re-render
    }).catch(() => {});
  }, [customer?.customer_id]);
  // Camera capture state for auto try-on
  const [showCamera, setShowCamera] = useState(false);
  const [pendingTryonProductId, setPendingTryonProductId] = useState<string | null>(null);
  const pendingTryonRef = useRef<string | null>(null);
  const lastTryonProductRef = useRef<string | null>(null);
  // Fetch product details for any [IMG:PROD-XXX] tags in messages.
  // Use a content hash to detect when new product IDs appear (not just message count).
  const lastContentRef = useRef("");
  useEffect(() => {
    const allContent = messages.map(m => m.content).join("|");
    if (allContent === lastContentRef.current) return;
    lastContentRef.current = allContent;

    const pids = new Set<string>();
    messages.forEach((m) => {
      for (const match of m.content.matchAll(/\[IMG:(PROD-\d+)\]/g)) pids.add(match[1]);
    });
    let fetched = 0;
    const toFetch = Array.from(pids).filter(pid => !productCache.current[pid] || productCache.current[pid].loading);
    toFetch.forEach((pid) => {
      productCache.current[pid] = { loading: true };
      api.getProduct(pid).then((p) => {
        productCache.current[pid] = p;
        fetched++;
        if (fetched >= toFetch.length) setProductCacheVer((v) => v + 1);
      }).catch(() => { delete productCache.current[pid]; });
    });
    // Also trigger re-render if all were already cached (images should show immediately)
    if (toFetch.length === 0 && pids.size > 0) setProductCacheVer((v) => v + 1);
  }, [messages]);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  // Reset photo upload state when customer changes
  useEffect(() => {
    setPhotoUploaded(false); setPhotoUrl("");
    if (customer?.customer_id) {
      api.checkPhoto(customer.customer_id).then((r) => {
        if (r.exists || (r as any).has_photo) { setPhotoUploaded(true); setPhotoUrl(r.url); }
      }).catch(() => {});
    }
  }, [customer?.customer_id]);

  useEffect(() => {
    if (!postCheckoutTime) return;
    setCustomer(null);
    setMessages((prev) => {
      const kept: typeof prev = [];
      for (let i = prev.length - 1; i >= 0 && kept.length < 3; i--) {
        if (prev[i].role === "assistant") kept.unshift(prev[i]);
      }
      kept.push({ role: "assistant", content: "Welcome to StoreAI! Sign in with your phone number to start shopping, or just say hi to browse our collection." });
      return kept;
    });
    setConnected(false);
  }, [postCheckoutTime, setCustomer]);

  // Load booth queue for tryon station
  useEffect(() => {
    if (cfg.demoMode !== "booth" || cfg.boothStation !== "tryon") return;
    const loadQueue = () => api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
    loadQueue();
    const interval = setInterval(loadQueue, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, [cfg.demoMode, cfg.boothStation]);

  const parseMsg = (m: any): ExtMsg => {
    const c: string = m.content || "";
    const tryonMatch = c.match(/<!--tryonUrl:(.+?)-->/);
    const latMatch = c.match(/<!--latency:(.+?)-->/);
    return {
      role: m.role,
      content: c.replace(/\n<!--tryonUrl:.+?-->\n<!--latency:.*?-->/, ""),
      ...(tryonMatch && { tryonUrl: tryonMatch[1] }),
      ...(latMatch?.[1] && { latency: latMatch[1] }),
    };
  };

  useEffect(() => {
    if (!cfg.saveChat) return;
    api.loadChatHistory(sessionId).then((r) => {
      const msgs = r?.messages || r?.items || [];
      if (msgs.length) setMessages(msgs.map(parseMsg));
      if (r?.last_key) setLastKey(r.last_key);
      // Restore session cost from saved messages
      cfg.resetCost();
      for (const m of msgs) {
        const c = m.cost;
        if (!c) continue;
        if (c.llm?.usd) cfg.addCost("llm", Number(c.llm.usd), { calls: 1 });
        if (c.infra?.usd) cfg.addCost("infra", Number(c.infra.usd));
      }
    }).catch(() => {});
  }, []);

  const loadOlder = async () => {
    if (!lastKey) return;
    const r = await api.loadChatHistory(sessionId, 10, lastKey);
    const older = (r?.messages || []).map(parseMsg);
    if (older.length) setMessages((prev) => [...older, ...prev]);
    setLastKey(r?.last_key || null);
  };

  const saveMsg = useCallback((role: string, content: string, extra?: { tryonUrl?: string; latency?: string; cost?: any }) => {
    if (!cfg.saveChat) return;
    const saveContent = extra?.tryonUrl ? `${content}\n<!--tryonUrl:${extra.tryonUrl}-->\n<!--latency:${extra.latency || ""}-->` : content;
    api.saveChatMessage({ session_id: sessionId, role, content: saveContent, customer_id: customer?.customer_id, cost: extra?.cost }).catch(() => {});
  }, [cfg.saveChat, sessionId, customer]);

  const customerRef = useRef(customer);
  const pendingClearRef = useRef(false);
  useEffect(() => { customerRef.current = customer; }, [customer]);

  // Booth try-on station: poll queue every 20s
  useEffect(() => {
    if (cfg.demoMode !== "booth" || cfg.boothStation !== "tryon") return;
    const fetch = () => api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
    fetch();
    const id = setInterval(fetch, 20000);
    return () => clearInterval(id);
  }, [cfg.demoMode, cfg.boothStation]);

  const detectSignIn = useCallback(async (toolResults: any[]) => {
    if (customerRef.current) return;
    for (const tr of toolResults) {
      // Check any tool result that contains a customer_id
      try {
        const data = JSON.parse(tr.result);
        // Skip error responses
        if (data.error) continue;
        
        if (data.customer_id) {
          if (data.first_name) {
            // Full profile available (getCustomerProfile returns this)
            setCustomer(data);
            setPostCheckoutTime(null);
            setPhotoUploaded(false);
            return;
          }
          // Partial data (registerCustomer returns customer_id + name but no first_name)
          // Fetch the full profile
          try {
            const profile = await api.signIn({ customer_id: data.customer_id });
            if (profile?.customer_id) {
              setCustomer(profile);
              setPostCheckoutTime(null);
              setPhotoUploaded(false);
              return;
            }
          } catch {}
        }
      } catch {}
    }
  }, [setCustomer]);

  const handlePostCheckout = useCallback(async (reply: string) => {
    if (!reply.includes("[CHECKOUT_COMPLETE]") || !customer) return;
    try {
      const qr = await api.generateTryOnQr(customer.customer_id);
      if (qr?.qr_code_base64) {
        const qrTag = `[TRYON_QR:${qr.qr_code_base64}]`;
        setMessages((prev) => {
          // Try to replace [QR_PLACEHOLDER] in the last assistant message
          const last = [...prev];
          for (let i = last.length - 1; i >= 0; i--) {
            if (last[i].role === "assistant" && last[i].content.includes("[QR_PLACEHOLDER]")) {
              last[i] = { ...last[i], content: last[i].content.replace("[QR_PLACEHOLDER]", `Scan the QR code to take your try-on photos with you:\n\n${qrTag}`) };
              return last;
            }
          }
          // Fallback: append as new message
          return [...prev, { role: "assistant", content: `Scan this QR code to view your try-on photos:\n\n${qrTag}` }];
        });
      }
    } catch {}
  }, [customer]);

  const detectSignOut = useCallback((reply: string) => {
    if (!customerRef.current) return;
    if (reply.includes("[LOGOUT]")) {
      setCustomer(null);
      setSessionId(resetSessionId());
    }
  }, [setCustomer]);

  const sendToAvatar = useCallback((text: string) => {
    if (!cfg.liveAvatarEnabled) return;
    try {
      const ws = new WebSocket(avatarSenderUrl(idToken));
      ws.onopen = () => { ws.send(JSON.stringify({ type: "speak", text })); ws.close(); };
      setAvatarSpeaking(true);
    } catch {}
  }, [cfg.liveAvatarEnabled, idToken]);

  const interruptAvatar = useCallback(() => {
    if (!cfg.liveAvatarEnabled) return;
    try {
      const ws = new WebSocket(avatarSenderUrl(idToken));
      ws.onopen = () => { ws.send(JSON.stringify({ type: "interrupt" })); ws.close(); };
    } catch {}
    setAvatarSpeaking(false);
  }, [cfg.liveAvatarEnabled, idToken]);

  // Avatar barge-in: monitor mic for sustained speech, interrupt avatar if detected
  const avatarBargeInRef = useRef<(() => void) | null>(null);
  const startAvatarBargeIn = useCallback(() => {
    if (!cfg.liveAvatarEnabled) return;
    // Stop any existing monitor
    avatarBargeInRef.current?.();
    let stopped = false;
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } }).then(async (stream) => {
      if (stopped) { stream.getTracks().forEach(t => t.stop()); return; }
      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      let speechStart = 0;
      const THRESHOLD = 30; // energy threshold
      const SUSTAIN_MS = 750; // must sustain speech for this long
      const check = () => {
        if (stopped) return;
        analyser.getByteFrequencyData(buf);
        const avg = buf.reduce((a, b) => a + b, 0) / buf.length;
        if (avg > THRESHOLD) {
          if (!speechStart) speechStart = Date.now();
          else if (Date.now() - speechStart > SUSTAIN_MS) {
            interruptAvatar();
            window.speechSynthesis?.cancel();
            cleanup();
            return;
          }
        } else {
          speechStart = 0;
        }
        requestAnimationFrame(check);
      };
      const cleanup = () => {
        stopped = true;
        src.disconnect(); stream.getTracks().forEach(t => t.stop());
        ctx.close().catch(() => {});
      };
      avatarBargeInRef.current = cleanup;
      check();
      // Auto-stop after 60s (avatar won't speak longer than that)
      setTimeout(() => { if (!stopped) cleanup(); }, 60000);
    }).catch(() => {});
  }, [cfg.liveAvatarEnabled, interruptAvatar]);

  const handlePhotoUpload = async (file: File) => {
    if (!customer) { toast("Sign in first to upload a photo", "info"); return; }
    setPhotoUploading(true);
    try {
      const b64 = await new Promise<string>((r) => { const rd = new FileReader(); rd.onload = () => r((rd.result as string).split(",")[1]); rd.readAsDataURL(file); });
      await api.uploadTryonPhoto({ customer_id: customer.customer_id, photo_base64: b64 });
      setPhotoUploaded(true);
      setPhotoUrl(URL.createObjectURL(file));
      toast("Photo uploaded — the assistant can now generate try-on images");
    } catch { toast("Failed to upload photo", "error"); }
    finally { setPhotoUploading(false); }
  };

  // Camera capture → upload → auto try-on (cloned from V1 with ref fix)
  const handleCameraCapture = async (base64: string) => {
    setShowCamera(false);
    if (!customer) return;
    // Read pending product from ref (survives re-renders) with state fallback
    const pid = pendingTryonRef.current || pendingTryonProductId || lastTryonProductRef.current;
    setPendingTryonProductId(null);
    pendingTryonRef.current = null;

    const pName = pid ? (productCache.current[pid]?.name || pid) : "item";
    setPhotoUploaded(true);

    try {
      // Upload the captured photo
      await api.uploadTryonPhoto({ customer_id: customer.customer_id, photo_base64: base64 });
      toast("Photo uploaded successfully!");

      if (!pid) {
        setMessages((prev) => [...prev, { role: "assistant" as const, content: "Photo uploaded! Now you can ask me to try on any product." }]);
        return;
      }

      // Auto-generate try-on immediately after photo upload
      const t0 = Date.now();
      setMessages((prev) => [...prev, { role: "assistant" as const, content: `Photo uploaded! Generating virtual try-on for ${pName}... This takes about 15 seconds.`, isLoading: true }]);

      const r = await api.generateTryOn(customer.customer_id, {
        product_id: pid,
        engine: cfg.vtonEngine,
        ...(cfg.vtonEngine === "qwen_image_edit" ? { vton_steps: cfg.vtonSteps, vton_cfg_scale: cfg.vtonCfgScale, vton_seed: cfg.vtonSeed } : {}),
      });
      const sec = ((Date.now() - t0) / 1000).toFixed(1);

      setMessages((prev) => {
        const filtered = prev.filter((m) => !m.isLoading);
        if (r?.result_url) {
          lastTryonProductRef.current = pid;
          const vtonCost = r.cost?.usd || 0;
          if (vtonCost) cfg.addCost("vton", vtonCost, { images: 1 });
          const engineName = ({ qwen_image_edit: "Qwen Image-Edit", fashn_vton: "FASHN" } as Record<string, string>)[r.engine || cfg.vtonEngine] || (r.engine || cfg.vtonEngine);
          const lat = `${engineName} · ${sec}s${vtonCost ? ` · $${vtonCost.toFixed(4)}` : ""}`;
          const txt = `Virtual try-on: ${pName}`;
          saveMsg("assistant", txt, { tryonUrl: r.result_url, latency: lat });
          return [...filtered, { role: "assistant" as const, content: txt, latency: lat, tryonUrl: r.result_url }];
        }
        return [...filtered, { role: "assistant" as const, content: `Try-on for ${pName} didn't return an image. Please try again.` }];
      });
    } catch (e: any) {
      toast(`Try-on failed: ${e.message}`, "error");
      setMessages((prev) => {
        const filtered = prev.filter((m) => !m.isLoading);
        return [...filtered, { role: "assistant" as const, content: `Sorry, the virtual try-on failed for ${pName}. Please try again.` }];
      });
    }
  };

  const sendMessage = async (text?: string) => {
    const msg = text || input.trim();
    if (!msg || loadingRef.current) {
      if (voiceTurnDoneRef.current) { voiceTurnDoneRef.current(); voiceTurnDoneRef.current = null; }
      return;
    }

    // Detect "retake photo" / "take my photo" / "new photo" requests — open camera directly
    const lower = msg.toLowerCase();
    const isCameraRequest = /\b(retake|re-take|take.*(my|a|another|new)\s*(photo|picture|pic|selfie)|new\s*(photo|picture|pic)|click.*(photo|picture|pic)|capture.*(photo|picture|pic)|photo\s*again|open.*camera)\b/i.test(lower);
    if (isCameraRequest) {
      setInput("");
      const userMsg: ExtMsg = { role: "user", content: msg };
      setMessages((prev) => [...prev, userMsg]);
      saveMsg("user", msg);
      setMessages((prev) => [...prev, { role: "assistant", content: "I'll open the camera for you now. Stand in the frame — full body, head to toe — and say \"click\" when ready." }]);
      pendingTryonRef.current = lastTryonProductRef.current;
      setPendingTryonProductId(lastTryonProductRef.current);
      setShowCamera(true);
      if (voiceTurnDoneRef.current) { voiceTurnDoneRef.current(); voiceTurnDoneRef.current = null; }
      return;
    }

    setInput("");
    // Clear previous customer's farewell message when new interaction starts
    if (pendingClearRef.current && messages.length > 0) { setMessages([]); pendingClearRef.current = false; }
    const userMsg: ExtMsg = { role: "user", content: msg };
    setMessages((prev) => [...prev, userMsg]);
    saveMsg("user", msg);
    setLoading(true);
    loadingRef.current = true;
    const t0 = Date.now();

    try {
      // Use streaming endpoint — call ALB directly to avoid CloudFront SSE buffering
      const res = await fetch(`${STREAM_API}/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(idToken ? { Authorization: idToken } : {}) },
        body: JSON.stringify({
          message: msg,
          model: cfg.chatModel,
          history: messages.slice(-20).map((m) => ({ role: m.role, content: m.content })),
          customer_id: customer?.customer_id || null,
          customer_name: customer ? `${customer.first_name} ${customer.last_name}` : null,
          demo_mode: cfg.demoMode,
          station: cfg.boothStation,
          item_cap: cfg.demoMode === "booth" ? cfg.boothItemCap : undefined,
          auto_queue_signin: cfg.demoMode === "booth" && cfg.boothStation === "tryon" ? cfg.autoQueueSignIn : undefined,
          engine: cfg.vtonEngine,
          ...(cfg.vtonEngine === "qwen_image_edit" ? { vton_steps: cfg.vtonSteps, vton_cfg_scale: cfg.vtonCfgScale, vton_seed: cfg.vtonSeed } : {}),
        }),
      });
      if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Request failed"); }

      // Stream tokens into a live message
      const streamMsg: ExtMsg = { role: "assistant", content: "", isLoading: true };
      setMessages((prev) => [...prev, streamMsg]);
      let ttfb = 0;
      let reply = "";
      let data: any = {};
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done: readerDone, value } = await reader.read();
        if (readerDone) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            const evt = JSON.parse(line.slice(6));
            if (evt.token) {
              if (!ttfb) ttfb = Date.now() - t0;
              reply += evt.token;
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last === streamMsg || last?.isLoading) {
                  return [...prev.slice(0, -1), { ...streamMsg, content: reply }];
                }
                return prev;
              });
            } else if (evt.done) {
              data = evt;
            }
          } catch {}
        }
      }
      // Remove all streaming/loading placeholders
      setMessages((prev) => prev.filter((m) => !m.isLoading));

      const totalSec = ((Date.now() - t0) / 1000).toFixed(1);
      const latency = ttfb ? `TTFB: ${(ttfb / 1000).toFixed(1)}s | Total: ${totalSec}s` : `${totalSec}s`;
      const costUsd = data.cost?.total_usd;
      const latencyWithCost = costUsd ? `${latency} · $${costUsd.toFixed(4)}` : latency;
      if (data.cost) {
        cfg.addCost("llm", data.cost.llm?.usd || 0, { calls: 1 });
        cfg.addCost("infra", data.cost.infra?.usd || 0);
      }

      await detectSignIn(data.tool_results || []);

      // Handle backend auto-sign-in from booth queue
      if (data.auto_signed_in && !customerRef.current) {
        const profile = await api.signIn({ customer_id: data.auto_signed_in.customer_id }).catch(() => null);
        if (profile?.customer_id) {
          setCustomer(profile);
          api.updateBoothStatus({ customer_id: profile.customer_id, status: "trying_on" }).catch(() => {});
          api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
        }
      }

      // Fallback: if still no customer after tool results, scan reply for customer ID
      if (!customerRef.current && reply) {
        const cidMatch = reply.match(/CUST-[A-Z0-9]{3,}/);
        if (cidMatch) {
          try {
            const profile = await api.signIn({ customer_id: cidMatch[0] });
            if (profile?.customer_id && profile?.first_name) {
              setCustomer(profile);
              setPostCheckoutTime(null);
              setPhotoUploaded(false);
            }
          } catch {}
        }
      }

      for (const tr of (data.tool_results || [])) {
        if (tr.tool === "searchProducts") {
          try {
            const d = JSON.parse(tr.result);
            for (const p of (d.products || [])) {
              if (p.product_id) productCache.current[p.product_id] = p;
            }
            setProductCacheVer((v) => v + 1);
          } catch {}
        }
      }

      // Collect virtualTryOn requests from tool results
      const deferredTryons: { product_id: string; pName: string }[] = [];
      let needsPhoto = false;
      let needsPhotoProductId = "";
      for (const tr of (data.tool_results || [])) {
        if (tr.tool === "virtual_tryon" || tr.tool === "virtualTryOn") {
          try {
            const result = typeof tr.result === "string" ? JSON.parse(tr.result) : tr.result;
            if (result.error === "no_photo" && tr.input?.product_id) {
              needsPhoto = true;
              needsPhotoProductId = tr.input.product_id;
            } else if (result.deferred && result.product_id && customer) {
              deferredTryons.push({ product_id: result.product_id, pName: productCache.current[result.product_id]?.name || result.product_id });
            }
          } catch {}
        }
      }

      // If try-on needs a photo, open camera automatically
      if (needsPhoto && customer) {
        lastTryonProductRef.current = needsPhotoProductId;
        pendingTryonRef.current = needsPhotoProductId;
        setPendingTryonProductId(needsPhotoProductId);
        setShowCamera(true);
      } else if (deferredTryons.length > 0 && customer && !photoUploaded) {
        lastTryonProductRef.current = deferredTryons[0].product_id;
        pendingTryonRef.current = deferredTryons[0].product_id;
        setPendingTryonProductId(deferredTryons[0].product_id);
        setShowCamera(true);
      }

      // Process try-ons: sequential for qwen_image_edit (single Neuron inference at a time), parallel for other engines
      if (deferredTryons.length > 0 && customer && photoUploaded) {
        // Fire photo-based size recommendation in parallel with first VTON
        const sizeRecPromise = api.photoSizeRecommendation({
          customer_id: customer.customer_id,
          product_id: deferredTryons[0].product_id,
          marker_color: cfg.markerColor,
          marker_height_cm: cfg.markerHeight,
        }).catch(() => null);

        const processTryon = async (item: { product_id: string; pName: string }, isFirst: boolean) => {
          const t0 = Date.now();
          const selectedEngine = cfg.vtonEngine;  // Try-On Engine dropdown value at request time
          const loadingMsg = { role: "assistant" as const, content: `Generating virtual try-on for ${item.pName}...`, isLoading: true };
          setMessages((prev) => [...prev, loadingMsg]);
          try {
            const r = await api.generateTryOn(customer.customer_id, {
              product_id: item.product_id, engine: selectedEngine,
              ...(selectedEngine === "qwen_image_edit" ? { vton_steps: cfg.vtonSteps, vton_cfg_scale: cfg.vtonCfgScale, vton_seed: cfg.vtonSeed } : {}),
            });
            const sec = ((Date.now() - t0) / 1000).toFixed(1);
            // Get size recommendation result if this is the first VTON
            let sizeText = "";
            if (isFirst && r?.result_url) {
              const sizeResult = await sizeRecPromise;
              if (sizeResult?.recommended_size) {
                sizeText = `\n\nSize recommendation: ${sizeResult.recommendation}`;
                setSizeRec(sizeResult);
              }
            }
            setMessages((prev) => {
              const filtered = prev.filter((m) => m !== loadingMsg);
              if (r?.result_url) {
                lastTryonProductRef.current = item.product_id;
                const vtonCost = r.cost?.usd || 0;
                const engineName = ({ qwen_image_edit: "Qwen Image-Edit", fashn_vton: "FASHN" } as Record<string, string>)[r.engine || selectedEngine] || (r.engine || selectedEngine);
                const lat = `${engineName} · ${sec}s${vtonCost ? ` · $${vtonCost.toFixed(4)}` : ""}`;
                if (vtonCost) cfg.addCost("vton", vtonCost, { images: 1 });
                const txt = `Virtual try-on: ${item.pName}${sizeText}`;
                saveMsg("assistant", txt, { tryonUrl: r.result_url, latency: lat });
                return [...filtered, { role: "assistant" as const, content: txt, latency: lat, tryonUrl: r.result_url }];
              }
              if (r?.error === "content_review") {
                return [...filtered, { role: "assistant" as const, content: r.message || "This virtual try-on couldn't be completed for this item. Please try a different product or photo." }];
              }
              return [...filtered, { role: "assistant" as const, content: `Try-on for ${item.pName} didn't return an image. Please try again.` }];
            });
          } catch (err: any) {
            const errMsg = err?.message || "";
            if (errMsg.toLowerCase().includes("no photo") || errMsg.toLowerCase().includes("upload")) {
              setMessages((prev) => {
                const filtered = prev.filter((m) => m !== loadingMsg);
                return [...filtered, { role: "assistant" as const, content: `I need your photo to generate the virtual try-on for ${item.pName}. Please upload one using the photo button above the chat, then try again.` }];
              });
            } else {
              setMessages((prev) => {
                const filtered = prev.filter((m) => m !== loadingMsg);
                return [...filtered, { role: "assistant" as const, content: `Sorry, the virtual try-on failed for ${item.pName}. Please try again.` }];
              });
            }
          }
        };

        if (cfg.vtonEngine === "qwen_image_edit") {
          // Sequential: Neuron can only handle one inference at a time
          (async () => { for (let i = 0; i < deferredTryons.length; i++) await processTryon(deferredTryons[i], i === 0); })();
        } else {
          deferredTryons.forEach((item, i) => processTryon(item, i === 0));
        }
      }

      detectSignOut(reply);

      let display = reply;
      for (const m of ["[RESET_CHAT]", "[SEND_TO_TRYON]", "[NEXT_IN_QUEUE]", "[CHECKOUT_COMPLETE]", "[LOGOUT]"]) {
        display = display.replaceAll(m, "");
      }
      display = display.trim();

      // For checkout: generate QR and replace placeholder BEFORE adding message
      if (reply.includes("[CHECKOUT_COMPLETE]") && customer && display.includes("[QR_PLACEHOLDER]")) {
        try {
          const qr = await api.generateTryOnQr(customer.customer_id);
          if (qr?.qr_code_base64) {
            display = display.replace("[QR_PLACEHOLDER]", `[TRYON_QR:${qr.qr_code_base64}]`);
          } else {
            display = display.replace("[QR_PLACEHOLDER]", "");
          }
        } catch { display = display.replace("[QR_PLACEHOLDER]", ""); }
      }

      // If this reply embeds a try-on image, prefix the metadata with the selected VTON engine.
      let latDisplay = latencyWithCost;
      if (/\[TRYON_?IMG:/.test(display)) {
        const eng = ({ qwen_image_edit: "Qwen Image-Edit", fashn_vton: "FASHN" } as Record<string, string>)[cfg.vtonEngine] || cfg.vtonEngine;
        latDisplay = `${eng} · ${latencyWithCost}`;
      }
      const assistantMsg: ExtMsg = { role: "assistant", content: display, latency: latDisplay };
      setMessages((prev) => [...prev, assistantMsg]);
      saveMsg("assistant", display, { cost: data.cost });
      setConnected(true);

      if (reply.includes("[CHECKOUT_COMPLETE]")) {
        setPostCheckoutTime(Date.now());
        setSessionId(resetSessionId());
      }

      // Process markers BEFORE voice/avatar calls (which can throw and skip these)
      refreshCounts();
      // Refresh cart size cache for product cards
      if (customer) api.getCart(customer.customer_id).then((r) => {
        const map: Record<string, string> = {};
        for (const item of (r.items || [])) map[item.product_id] = item.size;
        cartSizeCache.current = map;
      }).catch(() => {});
      if (reply.includes("[RESET_CHAT]")) setMessages([]);
      if (reply.includes("[SEND_TO_TRYON]") && cfg.demoMode === "booth" && cfg.boothStation === "shopping" && customer) {
        api.updateBoothStatus({ customer_id: customer.customer_id, status: "ready_for_tryon" }).catch(() => {});
        api.saveSelectionCost(customer.customer_id, cfg.sessionCost).catch(() => {});
        // Full reset for next customer — clear everything
        setMessages([]);
        setCustomer(null);
        setConnected(false);
        setSessionId(resetSessionId());
        cfg.resetCost();
        productCache.current = {};
        cartSizeCache.current = {};
        setProductCacheVer((v) => v + 1);
        setPhotoUploaded(false);
        api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
      }
      if (reply.includes("[NEXT_IN_QUEUE]") && cfg.demoMode === "booth" && cfg.boothStation === "tryon") {
        try {
          const q = await api.getBoothQueue();
          const queue = q.queue || [];
          if (queue.length > 0) {
            const next = await api.signIn({ customer_id: queue[0].customer_id });
            if (next?.customer_id) {
              setCustomer(next);
              setPostCheckoutTime(null);
              // Load selection cost from customer profile
              api.getCustomerProfile(next.customer_id).then((p) => {
                const sc = p?.selection_cost;
                if (sc) cfg.setSelectionCost(Number(sc.llm || 0) + Number(sc.vton || 0) + Number(sc.stt || 0) + Number(sc.infra || 0));
              }).catch(() => {});
            }
            setMessages([]); setConnected(false);
          }
        } catch {}
      }
      if (reply.includes("[CHECKOUT_COMPLETE]") && cfg.demoMode === "booth" && cfg.boothStation === "tryon" && customer) {
        api.updateBoothStatus({ customer_id: customer.customer_id, status: "completed" }).catch(() => {});
        // Clear caches for this customer so next customer starts fresh
        productCache.current = {};
        cartSizeCache.current = {};
        setProductCacheVer((v) => v + 1);
      }
      if (reply.includes("[LOGOUT]") && customer) {
        // Safeguard: on shopping station, if LLM sends LOGOUT but customer has items, treat as SEND_TO_TRYON
        let convertedToTryon = false;
        if (cfg.demoMode === "booth" && cfg.boothStation === "shopping" && !reply.includes("[SEND_TO_TRYON]")) {
          try {
            const [cart, tryon] = await Promise.all([api.getCart(customer.customer_id), api.getTryOnRoom(customer.customer_id)]);
            if ((cart.items?.length || 0) + (tryon.items?.length || 0) > 0) {
              api.updateBoothStatus({ customer_id: customer.customer_id, status: "ready_for_tryon" }).catch(() => {});
              convertedToTryon = true;
            }
          } catch {}
        }
        // Full reset for next customer
        setMessages([]);
        setCustomer(null);
        setConnected(false);
        setSessionId(resetSessionId());
        cfg.resetCost();
        productCache.current = {};
        cartSizeCache.current = {};
        setProductCacheVer((v) => v + 1);
        setPhotoUploaded(false);
        if (convertedToTryon) {
          api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
        }
        cfg.resetCost();
      }

      sendToAvatar(display);

      if (cfg.sttEngine === "nova-sonic" && display && assistantAudio) {
        const speakText = condenseForSpeech(display);
        if (speakText) {
          // Barge-in: resume listening during TTS so user can interrupt
          if (novaSonic.sessionActive) novaSonic.resumeListening();
          await novaSonic.speak(speakText, cfg.novaSonicVoice);
          // If speak completed normally (not interrupted), ensure listening is active
          if (novaSonic.sessionActive) novaSonic.resumeListening();
        }
      } else if (cfg.sttEngine === "nova-sonic" && novaSonic.sessionActive) {
        // No TTS but session active — resume listening
        novaSonic.resumeListening();
      }

      // Voice session: speak the condensed response using browser TTS
      if (voiceActiveRef.current && cfg.sttEngine !== "nova-sonic" && display && assistantAudio) {
        await voiceSpeak(display);
      }

      // Signal voice loop that this turn is complete
      if (voiceTurnDoneRef.current) {
        voiceTurnDoneRef.current();
        voiceTurnDoneRef.current = null;
      }
    } catch (e: any) {
      const errMsg = cfg.debugMode ? `Error: ${e.message}` : "Sorry, I encountered an error. Please try again.";
      setMessages((prev) => [...prev, { role: "assistant", content: errMsg }]);
    } finally {
      setLoading(false);
      loadingRef.current = false;
      // Signal voice loop on error/completion
      if (voiceTurnDoneRef.current) {
        voiceTurnDoneRef.current();
        voiceTurnDoneRef.current = null;
      }
    }
  };

  // --- Persistent voice session (engine-agnostic) ---
  const voiceActiveRef = useRef(false);
  const [voiceListening, setVoiceListening] = useState(false);
  const [voiceSpeaking, setVoiceSpeaking] = useState(false);
  const voiceSpeakingRef = useRef(false);
  useEffect(() => { voiceSpeakingRef.current = voiceSpeaking; }, [voiceSpeaking]);
  const sendMessageRef = useRef(sendMessage);
  useEffect(() => { sendMessageRef.current = sendMessage; }, [sendMessage]);
  // Signal for voice loop to know when sendMessage + TTS is fully done
  const voiceTurnDoneRef = useRef<(() => void) | null>(null);

  /**
   * Condense assistant text for natural speech.
   * Strips product listings, metadata, prices, IDs — keeps only the conversational bits.
   */
  const condenseForSpeech = useCallback((text: string): string => {
    let s = text;
    // Remove system markers
    s = s.replace(/\[(IMG|TRYON_?IMG|TRYON_QR|CHECKOUT_COMPLETE|SEND_TO_TRYON|NEXT_IN_QUEUE|LOGOUT|RESET_CHAT|QR_PLACEHOLDER)[^\]]*\]/g, "");
    // Remove markdown
    s = s.replace(/\*\*(.+?)\*\*/g, "$1").replace(/\*(.+?)\*/g, "$1").replace(/__(.+?)__/g, "$1").replace(/_(.+?)_/g, "$1");
    // Remove numbered list product entries: "1. Product Name by Brand — $29.99"
    s = s.replace(/^\d+\.\s+.+?(?:\$[\d.]+|by\s+\w+).*/gm, "");
    // Remove bullet lines with metadata: "- Price: $29.99", "- Sizes: S, M, L", "- Rating: 4.5"
    s = s.replace(/^\s*[-•*]\s*(Price|Sizes?|Rating|Brand|Colors?|Material|Available|Description)[^\n]*/gim, "");
    // Remove standalone price mentions like "$29.99"
    s = s.replace(/\$\d+\.\d{2}/g, "");
    // Remove product IDs
    s = s.replace(/PROD-\d+/g, "");
    // Remove size lists like "XS, S, M, L, XL, XXL"
    s = s.replace(/\b[A-Z]{1,3}(,\s*[A-Z]{1,3}){2,}\b/g, "");
    // Collapse whitespace
    s = s.replace(/\n{2,}/g, ". ").replace(/\n/g, " ").replace(/\s{2,}/g, " ").trim();
    // Remove trailing dots/spaces
    s = s.replace(/\.\s*\.\s*/g, ". ").replace(/\s+\./g, ".").trim();

    // If the condensed text is very long (product-heavy response), summarize further
    // Keep only the first ~200 chars which is usually the conversational intro
    if (s.length > 300) {
      // Find a natural break point (sentence end) near 200 chars
      const cutoff = s.indexOf(". ", 150);
      if (cutoff > 0 && cutoff < 300) {
        s = s.slice(0, cutoff + 1);
      }
    }

    return s;
  }, []);

  /** Speak text using browser speechSynthesis or Nova Sonic TTS */
  const voiceSpeak = useCallback(async (text: string) => {
    const clean = condenseForSpeech(text);
    if (!clean) return;

    if (cfg.sttEngine === "nova-sonic") {
      setVoiceSpeaking(true);
      try { await novaSonic.speak(clean, cfg.novaSonicVoice); } catch {}
      setVoiceSpeaking(false);
      return;
    }

    // Browser speechSynthesis with barge-in detection
    return new Promise<void>((resolve) => {
      if (!window.speechSynthesis) { resolve(); return; }
      window.speechSynthesis.cancel();
      const utt = new SpeechSynthesisUtterance(clean);
      utt.rate = 1.05;
      utt.pitch = 1.0;
      setVoiceSpeaking(true);

      // Barge-in: listen for user speech during TTS
      const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      let bargeRec: any = null;
      if (SR) {
        bargeRec = new SR();
        bargeRec.continuous = false;
        bargeRec.interimResults = true;
        let bargeTriggered = false;
        bargeRec.onresult = (e: any) => {
          const text = Array.from(e.results).map((r: any) => r[0].transcript).join("").trim();
          if (text.length > 3 && !bargeTriggered) {
            bargeTriggered = true;
            window.speechSynthesis.cancel();
            interruptAvatar();
            setVoiceSpeaking(false);
            resolve();
          }
        };
        bargeRec.onerror = () => {};
        try { bargeRec.start(); } catch {}
      }

      const done = () => { if (bargeRec) try { bargeRec.stop(); } catch {} setVoiceSpeaking(false); resolve(); };
      utt.onend = done;
      utt.onerror = done;
      window.speechSynthesis.speak(utt);
    });
  }, [cfg.sttEngine, cfg.novaSonicVoice, novaSonic]);

  /** Start one listening turn using Web Speech API. Returns when user finishes speaking. */
  const listenOneTurn = useCallback((): Promise<string> => {
    // Nova Sonic engine path — open a Nova /stt turn (mic → Bedrock ASR).
    // Without this, selecting Nova Sonic silently fell back to browser SpeechRecognition
    // (and hung on browsers without Web Speech, e.g. Firefox).
    if (cfg.sttEngine === "nova-sonic") {
      return new Promise((resolve) => {
        novaSonicRef.current.startSTT(
          (partial) => setInput(partial),
          (final) => { setVoiceListening(false); setInput(""); resolve(final || ""); },
          () => setVoiceListening(true),
        );
      });
    }
    // Whisper engine path
    if (cfg.sttEngine === "whisper") {
      return new Promise((resolve) => {
        navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true } }).then(async (stream) => {
          const audioCtx = new AudioContext({ sampleRate: 16000 });
          const actualRate = audioCtx.sampleRate;
          const needsResample = actualRate !== 16000;
          const source = audioCtx.createMediaStreamSource(stream);
          await audioCtx.audioWorklet.addModule("/pcm-processor.js");
          const worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
          const ws = new WebSocket(wsUrl(process.env.NEXT_PUBLIC_NOVA_SONIC_HOST, "/whisper") + (idToken ? `?token=${encodeURIComponent(idToken)}` : ""));
          let completed = "", pending = "";
          let silenceStart = 0;
          const SILENCE_TIMEOUT = 2000;
          let checkActiveTimer: any = null;
          let cleaned = false;
          const cleanup = () => {
            if (cleaned) return;
            cleaned = true;
            if (checkActiveTimer) clearInterval(checkActiveTimer);
            try { worklet.disconnect(); source.disconnect(); } catch {}
            stream.getTracks().forEach((t) => t.stop());
            audioCtx.close().catch(() => {});
            setTimeout(() => { try { ws.close(); } catch {} resolve((completed + pending).trim()); }, 300);
          };
          checkActiveTimer = setInterval(() => { if (!voiceActiveRef.current) cleanup(); }, 200);
          ws.onmessage = (e) => {
            try {
              const d = JSON.parse(e.data);
              if (d.completed) { completed += d.text + " "; pending = ""; }
              else { pending = d.text; }
              setInput(completed + pending);
              if ((completed + pending).trim().length > 3) {
                if (voiceSpeakingRef.current) window.speechSynthesis?.cancel();
                interruptAvatar();
              }
            } catch {}
          };
          ws.onopen = () => {
            setVoiceListening(true);
            worklet.port.onmessage = (e) => {
              if (ws.readyState !== 1) return;
              let f32 = e.data as Float32Array;
              if (needsResample) {
                const ratio = actualRate / 16000;
                const out = new Float32Array(Math.floor(f32.length / ratio));
                for (let i = 0; i < out.length; i++) out[i] = f32[Math.floor(i * ratio)];
                f32 = out;
              }
              let energy = 0;
              for (let i = 0; i < f32.length; i++) energy += f32[i] * f32[i];
              energy = Math.sqrt(energy / f32.length);
              if (energy > 0.01) { silenceStart = 0; }
              else if (!silenceStart) { silenceStart = Date.now(); }
              else if (Date.now() - silenceStart > SILENCE_TIMEOUT && (completed + pending).trim()) { cleanup(); return; }
              const i16 = new Int16Array(f32.length);
              for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
              ws.send(i16.buffer);
            };
            source.connect(worklet);
            worklet.connect(audioCtx.destination);
          };
          ws.onerror = () => cleanup();
          ws.onclose = () => cleanup();
        }).catch(() => resolve(""));
      });
    }

    // Web Speech API path (default / browser)
    // Uses continuous mode — keeps listening until speech is detected and finalized.
    // Auto-restarts on any error to ensure uninterrupted demo experience.
    return new Promise((resolve) => {
      const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      if (!SR) { resolve(""); return; }

      const rec = new SR();
      rec.lang = "en-US";
      rec.interimResults = true;
      rec.continuous = true;
      rec.maxAlternatives = 1;

      let finalText = "";
      let resolved = false;
      const finish = (text: string) => {
        if (resolved) return;
        resolved = true;
        try { rec.stop(); } catch {}
        setInput("");
        resolve(text);
      };

      rec.onresult = (e: any) => {
        const results = Array.from(e.results) as any[];
        const transcript = results.map((r: any) => r[0].transcript).join("");
        setInput(transcript);
        // Check if last result is final — user finished speaking
        const lastResult = results[results.length - 1] as any;
        if (lastResult.isFinal) {
          finalText = transcript.trim();
          if (finalText) finish(finalText);
        }
      };

      rec.onend = () => {
        // In continuous mode, onend fires when the browser kills the session (Chrome ~60s limit).
        // If we haven't captured final speech yet and voice is still active, just resolve empty
        // so the loop immediately restarts a fresh recognition instance.
        if (!resolved) {
          finish(finalText || "");
        }
      };

      rec.onerror = (e: any) => {
        // ALL errors resolve (never reject) — the voice loop will just retry.
        // This prevents the loop from dying on transient errors.
        if (!resolved) {
          finish("");
        }
      };

      try { rec.start(); } catch { finish(""); return; }
      setVoiceListening(true);
    });
  }, [cfg.sttEngine]);

  /** Main voice session loop — designed to NEVER die during a demo.
   *  Self-healing: any error just triggers a retry with no user intervention.
   *  Only stops when the user explicitly toggles voice off.
   */
  const runVoiceSession = useCallback(async () => {
    // Auto-fallback: if browser STT selected but Web Speech unavailable (Firefox), switch to Whisper
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR && cfg.sttEngine === "browser") {
      cfg.setSttEngine("whisper" as any);
      toast("Web Speech not available — using Whisper STT", "info");
    }
    voiceActiveRef.current = true;
    setVoiceChatMode("voice");
    setVoiceListening(false);

    let consecutiveEmptyTurns = 0;

    try {
      while (voiceActiveRef.current) {
        setVoiceListening(false);
        setInput("");
        let text = "";

        try {
          text = await listenOneTurn();
        } catch (err: any) {
          // NEVER let errors kill the loop — just retry
          if (!voiceActiveRef.current) break;
          consecutiveEmptyTurns++;
          // Brief backoff to avoid hammering on persistent errors
          await new Promise((r) => setTimeout(r, Math.min(consecutiveEmptyTurns * 200, 2000)));
          continue;
        }

        if (!voiceActiveRef.current) break;

        if (!text) {
          consecutiveEmptyTurns++;
          // After many empty turns, add a tiny delay to avoid CPU spin
          // (Chrome kills Web Speech after ~60s of silence, this handles the restart)
          if (consecutiveEmptyTurns > 5) {
            await new Promise((r) => setTimeout(r, 100));
          }
          continue;
        }

        // Got speech! Reset the counter and process it
        consecutiveEmptyTurns = 0;
        setVoiceListening(false);
        setInput("");

        // Create a promise that sendMessage will resolve when done (including TTS)
        const turnDone = new Promise<void>((resolve) => {
          voiceTurnDoneRef.current = resolve;
        });

        // Fire sendMessage (async, will signal voiceTurnDoneRef when complete)
        sendMessageRef.current(text);

        // Wait for the full turn to complete (with a safety timeout so we never get stuck)
        await Promise.race([
          turnDone,
          new Promise<void>((r) => setTimeout(r, 60000)),
        ]);

        if (!voiceActiveRef.current) break;

        // Small pause before next listen to avoid picking up TTS tail-end
        await new Promise((r) => setTimeout(r, 300));
      }
    } finally {
      voiceActiveRef.current = false;
      setVoiceChatMode("chat");
      setVoiceListening(false);
      setVoiceSpeaking(false);
      setInput("");
    }
  }, [listenOneTurn]);

  const toggleVoiceSession = useCallback(() => {
    if (voiceActiveRef.current) {
      // Stop — immediate, clean shutdown
      voiceActiveRef.current = false;
      setVoiceChatMode("chat");
      setVoiceListening(false);
      setVoiceSpeaking(false);
      setInput("");
      window.speechSynthesis?.cancel();
      sessionStorage.removeItem("storeai_voice_active");
      return;
    }
    // Start — fresh session, no stale state
    sessionStorage.setItem("storeai_voice_active", "1");
    // Ensure any previous loop is dead before starting new one
    voiceActiveRef.current = false;
    setTimeout(() => runVoiceSession(), 50);
  }, [runVoiceSession]);

  // Auto-restart voice session if it was active before navigation
  useEffect(() => {
    if (sessionStorage.getItem("storeai_voice_active") === "1" && !voiceActiveRef.current) {
      runVoiceSession();
    }
    return () => {
      // On unmount, keep sessionStorage but stop the loop cleanly
      voiceActiveRef.current = false;
    };
  }, [runVoiceSession]);

  const startListening = () => {
    if (listening) {
      if (cfg.sttEngine === "nova-sonic") novaSonic.stopSTT();
      setListening(false);
      return;
    }
    if (cfg.sttEngine === "nova-sonic") {
      setListening("connecting");
      novaSonic.startSTT(
        (partial) => {
          setInput(partial);
          // Barge-in: if assistant is speaking and user starts talking, interrupt
          if (partial.trim().length > 3 && novaSonic.speaking) {
            novaSonic.stopSpeaking();
            interruptAvatar();
          }
        },
        (final) => { setInput(""); setListening(false); sendMessage(final); },
        () => setListening(true),
      );
      return;
    }
    if (cfg.sttEngine === "whisper") {
      setListening(true);
      console.log("[Whisper] Starting...");
      navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true } }).then(async (stream) => {
        const audioCtx = new AudioContext({ sampleRate: 16000 });
        const actualRate = audioCtx.sampleRate;
        const needsResample = actualRate !== 16000;
        console.log(`[Whisper] AudioContext rate=${actualRate} resample=${needsResample}`);
        const source = audioCtx.createMediaStreamSource(stream);
        await audioCtx.audioWorklet.addModule("/pcm-processor.js");
        console.log("[Whisper] Worklet loaded");
        const worklet = new AudioWorkletNode(audioCtx, "pcm-processor");
        const ws = new WebSocket(wsUrl(process.env.NEXT_PUBLIC_NOVA_SONIC_HOST, "/whisper") + (idToken ? `?token=${encodeURIComponent(idToken)}` : ""));
        console.log(`[Whisper] WS connecting: ${wsUrl(process.env.NEXT_PUBLIC_NOVA_SONIC_HOST, "/whisper")}`);
        let completed = "", pending = "";
        let silenceStart = 0;
        const SILENCE_TIMEOUT = 2000;
        ws.onmessage = (e) => {
          try {
            const d = JSON.parse(e.data);
            if (d.completed) { completed += d.text + " "; pending = ""; }
            else { pending = d.text; }
            setInput(completed + pending);
            if (pending.trim().length > 3 && voiceSpeakingRef.current) {
              window.speechSynthesis?.cancel();
              interruptAvatar();
            }
          } catch {}
        };
        ws.onopen = () => {
          console.log("[Whisper] WS connected");
          let msgCount = 0;
          worklet.port.onmessage = (e) => {
            if (ws.readyState !== 1) return;
            msgCount++;
            if (msgCount <= 3) console.log(`[Whisper] Audio chunk #${msgCount} len=${e.data.length}`);
            let f32 = e.data as Float32Array;
            // Downsample to 16kHz if needed
            if (needsResample) {
              const ratio = actualRate / 16000;
              const out = new Float32Array(Math.floor(f32.length / ratio));
              for (let i = 0; i < out.length; i++) out[i] = f32[Math.floor(i * ratio)];
              f32 = out;
            }
            let energy = 0;
            for (let i = 0; i < f32.length; i++) energy += f32[i] * f32[i];
            energy = Math.sqrt(energy / f32.length);
            if (energy > 0.01) { silenceStart = 0; }
            else if (!silenceStart) { silenceStart = Date.now(); }
            else if (Date.now() - silenceStart > SILENCE_TIMEOUT && (completed + pending).trim()) { cleanup(); return; }
            const i16 = new Int16Array(f32.length);
            for (let i = 0; i < f32.length; i++) i16[i] = Math.max(-32768, Math.min(32767, f32[i] * 32768));
            ws.send(i16.buffer);
          };
          source.connect(worklet);
          worklet.connect(audioCtx.destination);
        };
        const cleanup = () => {
          worklet.disconnect(); source.disconnect();
          stream.getTracks().forEach((t) => t.stop());
          audioCtx.close();
          setTimeout(() => {
            ws.close();
            const result = (completed + pending).trim();
            setListening(false);
            if (result) sendMessage(result);
          }, 1000);
        };
        ws.onerror = (err) => { console.error("[Whisper] WS error:", err); cleanup(); setListening(false); };
        setTimeout(() => { if (ws.readyState === 1) cleanup(); }, 30000);
      }).catch((err) => { console.error("[Whisper] Failed:", err); setListening(false); });
      return;
    }
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return toast("Web Speech API not supported. Use Chrome or Edge.", "error");
    const rec = new SR();
    rec.lang = "en-US"; rec.interimResults = true;
    setListening(true);
    rec.onresult = (e: any) => {
      const t = Array.from(e.results).map((r: any) => r[0].transcript).join("");
      if (e.results[e.results.length - 1].isFinal) { setInput(""); if (t.trim()) sendMessage(t.trim()); }
      else { setInput(t); }
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    rec.start();
  };

  const runTryOn = async () => {
    if (!photoFile || !tryonPid || !customer) return;
    setTryonRunning(true);
    try {
      const buf = await photoFile.arrayBuffer();
      const b64 = btoa(new Uint8Array(buf).reduce((s, b) => s + String.fromCharCode(b), ""));
      await api.uploadTryonPhoto({ customer_id: customer.customer_id, photo_base64: b64 });
      const r = await api.generateTryOn(customer.customer_id, {
        product_id: tryonPid, engine: cfg.vtonEngine,
        ...(cfg.vtonEngine === "qwen_image_edit" ? { vton_steps: cfg.vtonSteps, vton_cfg_scale: cfg.vtonCfgScale, vton_seed: cfg.vtonSeed } : {}),
      });
      if (r?.result_url) setTryonResult(r.result_url);
      else if (r?.result_image) setTryonResult(`data:image/png;base64,${r.result_image}`);
      else toast("Try-on did not return an image", "error");
    } catch (e: any) { toast(`Try-on failed: ${e.message}`, "error"); }
    finally { setTryonRunning(false); }
  };

  /** Render message content with markdown parsing and horizontal product cards */
  const renderContent = (text: string, idx: number) => {
    const imgTags = text.match(/\[IMG:PROD-\w+\]/g) || [];
    const imgCount = imgTags.length;
    let cleaned = text;

    // Aggressively strip all metadata text around product [IMG:] tags
    if (imgCount >= 2) {
      // Strip numbered list headers: "1. **Product Name**" before [IMG:]
      cleaned = cleaned.replace(/^\d+\.\s*\*{0,2}[^[\n]*\*{0,2}\s*\n?\s*(?=\[IMG:)/gm, "");
      // Strip bold headers before [IMG:]
      cleaned = cleaned.replace(/^\*{2}[^*\n]+\*{2}:?\s*\n?\s*(?=\[IMG:)/gm, "");
      // Strip all lines between [IMG:] tags that contain product metadata
      cleaned = cleaned.replace(new RegExp("(\\[IMG:PROD-\\w+\\])[^\\[]*?(?=\\[IMG:PROD-|\\[TRYON_|$)", "gs"), "$1\n");
      // Strip "- Sizes:", "- Rating:", "- Price:", "- Brand:" etc
      cleaned = cleaned.replace(/^\s*[-•*]\s*(Sizes?|Rating|Price|Brand|Colors?|Material|Description|Available)[^\n]*/gim, "");
      // Strip standalone star rating lines
      cleaned = cleaned.replace(/^\s*⭐[^\n]*/gm, "");
      // Strip lines that are just size lists like "XS, S, M, L, XL"
      cleaned = cleaned.replace(/^\s*[A-Z]{1,3}(,\s*[A-Z]{1,3}){2,}[^\n]*/gm, "");
    }
    cleaned = cleaned.replace(/\n{3,}/g, "\n\n").trim();
    // Remove incomplete/truncated TRYON_IMG tags (no closing bracket) that show as broken images
    cleaned = cleaned.replace(/\[TRYON_?IMG:[^\]]*$/gm, "");
    // Remove duplicate tryon tags (keep only the last valid one)
    const tryonTags = cleaned.match(/\[TRYON_?IMG:[^\]]+\]/g) || [];
    if (tryonTags.length > 1) {
      // Keep only the last one (backend-injected complete URL)
      for (let t = 0; t < tryonTags.length - 1; t++) {
        cleaned = cleaned.replace(tryonTags[t], "");
      }
    }

    const parts = cleaned.split(/(\[IMG:PROD-\w+\]|\[TRYON_?IMG:[^\]]+\]|\[TRYON_QR:[^\]]+\]|\[QR_PLACEHOLDER\])/g);
    const elements: React.ReactNode[] = [];
    let productCards: React.ReactNode[] = [];

    const flushCards = () => {
      if (productCards.length === 0) return;
      elements.push(
        <div key={`scroll-${elements.length}`} style={{ display: "flex", gap: "8px", flexWrap: "wrap", margin: "8px 0" }}>
          {productCards.slice(0, 5)}
        </div>
      );
      productCards = [];
    };

    parts.forEach((part, i) => {
      const imgMatch = part.match(/\[IMG:(PROD-\w+)\]/);
      if (imgMatch) {
        const pid = imgMatch[1];
        const p = productCache.current[pid];
        const imgSrc = p?.image_file ? `/images/${p.image_file}` : (p?.image_url || "");
        const sizes = p?.sizes
          ? (Array.isArray(p.sizes) ? p.sizes : Object.keys(p.sizes))
          : (p?.available_sizes || []);
        productCards.push(
          <div key={i} style={{ flexShrink: 0, width: "152px", scrollSnapAlign: "start" }} className="border border-[hsl(0,0%,90%)] rounded-sm bg-white overflow-hidden hover:shadow-sm transition-shadow">
            {imgSrc && <img src={imgSrc} alt={p?.name || pid} className="w-full h-32 object-cover cursor-pointer hover:opacity-90" onClick={() => { setZoomed(false); setZoomImg(imgSrc); }} />}
            <div className="p-2">
              <div className="text-[9px] uppercase tracking-[1px] text-[hsl(0,0%,55%)]">{p?.brand}</div>
              <div className="text-[11px] font-medium leading-tight mt-0.5 line-clamp-2">{p?.name || pid}</div>
              <div className="flex items-center justify-between mt-1.5">
                <span className="text-[11px] font-semibold">{p?.price ? `$${Number(p.price).toFixed(2)}` : ""}</span>
                {p?.rating && (
                  <span className="text-[9px] text-[hsl(0,0%,55%)] flex items-center gap-0.5">
                    <svg className="h-2.5 w-2.5 fill-[hsl(45,93%,47%)]" viewBox="0 0 20 20"><path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"/></svg>
                    {p.rating}
                  </span>
                )}
              </div>
              {sizes.length > 0 && (
                <div className="text-[8px] text-[hsl(0,0%,60%)] mt-1 truncate">
                  {cartSizeCache.current[pid]
                    ? <span>Size: <strong className="text-[hsl(0,0%,30%)]">{cartSizeCache.current[pid]}</strong></span>
                    : sizes.join(" · ")}
                </div>
              )}
            </div>
          </div>
        );
        return;
      }
      const tryonMatch = part.match(/\[TRYON_?IMG:([^\]]+)\]/);
      if (tryonMatch) { flushCards(); const _turl = tryonMatch[1]; elements.push(<img key={i} src={api.tryonImgSrc(_turl)} alt="Try-on" className="max-w-xs rounded-sm mt-2 block cursor-pointer hover:opacity-90" onClick={() => { setZoomed(false); setZoomImg(_turl); }} />); return; }
      const qrMatch = part.match(/\[TRYON_QR:([^\]]+)\]/);
      if (qrMatch) { flushCards(); elements.push(<img key={i} src={`data:image/png;base64,${qrMatch[1]}`} alt="QR" className="w-48 mt-2 block" />); return; }
      if (part === "[QR_PLACEHOLDER]") { flushCards(); elements.push(<span key={i} className="text-xs text-[hsl(0,0%,55%)] animate-pulse block mt-2">Generating QR code...</span>); return; }
      const trimmed = part.trim();
      if (trimmed && trimmed.length > 2 && !/^[-•]\s*(Sizes?|Rating|Price|Brand):/i.test(trimmed) && !/^⭐/.test(trimmed)) {
        flushCards();
        elements.push(<span key={i} dangerouslySetInnerHTML={{ __html: parseMarkdown(part) }} />);
      }
    });
    flushCards();
    return elements;
  };
  if (cfg.voiceMode) {
    const wsUrl = `ws://${typeof window !== "undefined" ? window.location.hostname : "localhost"}:${NOVA_SONIC_PORT}/ws/nova-sonic`;
    return (
      <div className="flex flex-col h-[calc(100vh-3rem)] max-w-4xl mx-auto">
        <h1 className="text-3xl font-light tracking-tight text-[hsl(0,0%,12%)]">Voice Mode</h1>
        <p className="text-sm text-[hsl(0,0%,55%)] mb-4">Real-time speech-to-speech via Amazon Nova Sonic</p>
        <div className="flex-1 border border-[hsl(0,0%,90%)] rounded-sm bg-white overflow-hidden">
          <iframe src={`/nova-sonic.html?ws=${encodeURIComponent(wsUrl)}`}
            className="w-full h-full border-0" allow="microphone" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)] max-w-4xl mx-auto">
      {/* Header with photo upload on the right */}
      <div className="flex items-start justify-between mb-3">
        <div>
          <h1 className="text-3xl font-light tracking-tight text-[hsl(0,0%,12%)]">Shopping Assistant</h1>
          <p className="text-xs text-[hsl(0,0%,55%)] mt-0.5">
            {connected ? <span className="text-[hsl(142,71%,45%)]">● Connected — {cfg.chatModel}</span> : "Type to start a conversation"}
          </p>
        </div>

        {/* Photo upload — right side, attractive design */}
        {cfg.photoUploadEnabled && !(cfg.demoMode === "booth" && cfg.boothStation === "shopping") && (
          <div className="flex items-center gap-3">
            {customer ? (
              <label className={`flex items-center gap-2 px-4 py-2 rounded-sm border cursor-pointer transition-all ${
                photoUploaded
                  ? "border-[hsl(142,71%,45%)] bg-[hsl(142,71%,97%)] text-[hsl(142,71%,30%)]"
                  : "border-[hsl(0,0%,85%)] bg-white hover:border-[hsl(0,0%,60%)] text-[hsl(0,0%,30%)]"
              }`}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><path d="m21 15-5-5L5 21" />
                </svg>
                <span className="text-xs font-medium">
                  {photoUploading ? "Uploading..." : photoUploaded ? "Photo ready" : "Upload photo"}
                </span>
                <input type="file" accept="image/*" className="hidden" disabled={photoUploading}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) handlePhotoUpload(f); e.target.value = ""; }} />
              </label>
            ) : (
              <div className="text-[11px] text-[hsl(0,0%,55%)] italic">Sign in to upload photo</div>
            )}
            {photoUrl && (
              <img src={photoUrl} alt="Your photo" onClick={() => { setZoomed(false); setZoomImg(photoUrl); }}
                className="w-10 h-10 rounded-full object-cover border border-[hsl(0,0%,85%)] cursor-pointer hover:opacity-80 transition-opacity" />
            )}
          </div>
        )}
      </div>

      {/* Booth queue — tryon station */}
      {cfg.demoMode === "booth" && cfg.boothStation === "tryon" && (
        <div className="mb-3 p-4 border border-[hsl(0,0%,90%)] rounded-sm bg-white">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[11px] uppercase tracking-[2px] text-[hsl(0,0%,55%)] font-medium">Customer Queue</h3>
            <button onClick={() => api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {})}
              className="text-[11px] text-[hsl(0,0%,55%)] hover:text-[hsl(0,0%,12%)] transition-colors">Refresh</button>
          </div>
          {boothQueue.length > 0 ? (
            <div className="flex flex-col gap-2 max-h-[120px] overflow-y-auto">
              {boothQueue.map((q, i) => (
                <div key={q.customer_id} className="flex items-center text-sm gap-3">
                  <span className="text-[hsl(0,0%,30%)]">{i + 1}. {q.first_name} {q.last_name} ({q.phone}) — {q.total_items} items</span>
                  <button onClick={() => { api.updateBoothStatus({ customer_id: q.customer_id, status: "completed" }).then(() => api.getBoothQueue().then((r) => setBoothQueue(r.queue || []))).catch(() => {}); }}
                    title="Remove from queue"
                    className="text-[hsl(0,0%,70%)] hover:text-[hsl(0,72%,51%)] text-sm transition-colors">&times;</button>
                  <span className="flex-1" />
                  <button onClick={async () => {
                    const c = await api.signIn({ customer_id: q.customer_id }).catch(() => null);
                    if (c?.customer_id) {
                      setCustomer(c);
                      setPostCheckoutTime(null);
                      setPhotoUploaded(false);
                      api.updateBoothStatus({ customer_id: q.customer_id, status: "trying_on" }).catch(() => {});
                      api.getBoothQueue().then((r) => setBoothQueue(r.queue || [])).catch(() => {});
                      setMessages([]);
                      setConnected(false);
                      const newSid = resetSessionId();
                      setSessionId(newSid);
                      // Auto-send greeting to show customer's items
                      setTimeout(() => sendMessageRef.current(`Welcome ${c.first_name} to the try-on station. Show me my items.`), 300);
                    }
                  }} className="text-[11px] bg-[hsl(0,0%,12%)] text-white px-3 py-1 rounded-sm hover:bg-[hsl(0,0%,20%)] transition-colors">Start</button>
                </div>
              ))}
            </div>
          ) : <p className="text-xs text-[hsl(0,0%,55%)]">No customers in queue. Waiting for customers from the Selection station...</p>}
        </div>
      )}

      {/* Action buttons */}
      <div className="flex gap-3 mb-2">
        {lastKey && (
          <button onClick={loadOlder} className="text-[11px] text-[hsl(0,0%,55%)] hover:text-[hsl(0,0%,12%)] transition-colors">Load older messages</button>
        )}
        {messages.length > 0 && (
          <button onClick={() => { const ns = genSessionId(); localStorage.setItem("storeai_chat_sid", ns); setSessionId(ns); setMessages([]); setConnected(false); setLastKey(null); setCustomer(null); setPostCheckoutTime(null); }}
            className="text-[11px] text-[hsl(0,0%,55%)] hover:text-[hsl(0,72%,51%)] transition-colors">Clear chat</button>
        )}
      </div>

      {/* Chat messages */}
      <div className="flex-1 overflow-y-auto border border-[hsl(0,0%,90%)] rounded-sm bg-white p-4 flex flex-col gap-3" data-cache-ver={productCacheVer}>
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[80%] px-4 py-2.5 rounded-sm text-sm leading-relaxed ${
              msg.role === "user"
                ? "bg-[hsl(0,0%,12%)] text-white"
                : "bg-[hsl(40,10%,96%)] text-[hsl(0,0%,20%)]"
            }`}>
              {msg.isLoading ? (
                <span className="animate-pulse text-[hsl(0,0%,55%)]">{msg.content}</span>
              ) : msg.role === "assistant" ? (
                <div className="whitespace-pre-wrap">{renderContent(msg.content, i)}</div>
              ) : msg.content}
              {msg.tryonUrl && (
                <div className="mt-2">
                  <img src={api.tryonImgSrc(msg.tryonUrl)} alt="Try-on result" className="rounded-sm mt-2 cursor-pointer hover:opacity-90" style={{maxHeight: "50vh"}} onClick={() => { setZoomed(false); setZoomImg(msg.tryonUrl!); }} />
                </div>
              )}
              {msg.latency && (
                <div className="text-[10px] text-[hsl(0,0%,60%)] mt-1.5">{msg.latency}</div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-[hsl(40,10%,96%)] px-4 py-2.5 rounded-sm text-sm text-[hsl(0,0%,55%)]">
              <span className="inline-flex gap-1">
                <span className="w-1.5 h-1.5 bg-[hsl(0,0%,55%)] rounded-full animate-bounce" style={{animationDelay: "0ms"}} />
                <span className="w-1.5 h-1.5 bg-[hsl(0,0%,55%)] rounded-full animate-bounce" style={{animationDelay: "150ms"}} />
                <span className="w-1.5 h-1.5 bg-[hsl(0,0%,55%)] rounded-full animate-bounce" style={{animationDelay: "300ms"}} />
              </span>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Inline Try-On Panel */}
      {cfg.inlineTryon && tryonPid && (
        <div className="border border-[hsl(0,0%,90%)] rounded-sm bg-white p-4 mt-2">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-[hsl(0,0%,12%)]">Virtual Try-On — {tryonPid}</h3>
            <button onClick={() => { setTryonPid(null); setTryonResult(null); setSizeRec(null); }} className="text-[hsl(0,0%,60%)] hover:text-[hsl(0,0%,12%)] text-lg leading-none">&times;</button>
          </div>
          <div className="flex gap-4 flex-wrap items-end">
            <div>
              <label className="text-[11px] text-[hsl(0,0%,55%)] block mb-1">Your Photo</label>
              <input ref={photoRef} type="file" accept="image/*" onChange={(e) => setPhotoFile(e.target.files?.[0] || null)} className="text-xs" />
            </div>
            <div>
              <label className="text-[11px] text-[hsl(0,0%,55%)] block mb-1">Garment</label>
              <select value={tryonGarment} onChange={(e) => setTryonGarment(e.target.value)} className="text-xs border border-[hsl(0,0%,90%)] rounded-sm px-2 py-1.5">
                <option value="UPPER_BODY">Upper Body</option><option value="LOWER_BODY">Lower Body</option>
                <option value="FULL_BODY">Full Body</option><option value="FOOTWEAR">Footwear</option>
              </select>
            </div>
            <button onClick={runTryOn} disabled={!photoFile || !customer || tryonRunning}
              className="text-xs bg-[hsl(0,0%,12%)] text-white px-4 py-1.5 rounded-sm disabled:opacity-40 transition-colors hover:bg-[hsl(0,0%,20%)]">
              {tryonRunning ? "Generating..." : "Try it on"}
            </button>
          </div>
          {tryonResult && <img src={tryonResult} alt="Result" className="w-64 rounded-sm mt-3" />}
          <div className="mt-3 pt-3 border-t border-[hsl(0,0%,92%)]">
            <h4 className="text-[11px] font-medium text-[hsl(0,0%,55%)] mb-2">Size Recommendation</h4>
            {sizeRec ? (
              <div className="text-sm">
                <span className="font-medium">{sizeRec.recommendation}</span>
                {sizeRec.alternative && <span className="text-xs text-[hsl(0,0%,55%)] ml-2">{sizeRec.alternative.note}</span>}
                <button onClick={() => setSizeRec(null)} className="text-xs text-[hsl(0,0%,55%)] hover:text-[hsl(0,0%,12%)] ml-2">Reset</button>
              </div>
            ) : (
              <div className="text-xs text-[hsl(0,0%,55%)] italic">Size will be recommended automatically with your first try-on</div>
            )}
          </div>
        </div>
      )}

      {/* Input bar */}
      <div className="flex gap-2 mt-3 items-center">
        {/* Voice session toggle */}
        <button onClick={toggleVoiceSession}
          className={`px-3 py-2 rounded-sm text-sm border transition-colors flex-shrink-0 ${
            voiceChatMode === "voice" && voiceListening
              ? "bg-[hsl(0,72%,51%)] border-[hsl(0,72%,51%)] text-white"
              : voiceChatMode === "voice"
              ? "bg-[hsl(35,90%,50%)] border-[hsl(35,90%,50%)] text-white animate-pulse"
              : "border-[hsl(0,0%,85%)] hover:border-[hsl(0,0%,60%)] text-[hsl(0,0%,40%)]"
          }`}
          title={voiceChatMode === "voice" && voiceListening ? "Listening — click to stop" : voiceChatMode === "voice" ? "Connecting..." : "Start voice mode"}>
          {voiceChatMode === "voice" ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6" /></svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" x2="12" y1="19" y2="22" />
            </svg>
          )}
        </button>

        {/* Voice session active — show live transcript */}
        {voiceChatMode === "voice" ? (
          <div className="flex-1 border border-[hsl(0,0%,90%)] rounded-sm px-4 py-2 text-sm bg-[hsl(40,10%,96%)] flex items-center min-h-[40px]">
            {input ? (
              <span className="text-[hsl(0,0%,30%)]">{input}</span>
            ) : voiceSpeaking ? (
              <span className="text-[hsl(0,0%,55%)] italic">Speaking...</span>
            ) : loading ? (
              <span className="text-[hsl(0,0%,55%)] italic">Thinking...</span>
            ) : voiceListening ? (
              <span className="text-[hsl(0,0%,55%)] italic flex items-center gap-2">
                <span className="flex gap-0.5 items-end h-4">
                  <span className="w-1 h-2 bg-[hsl(0,72%,51%)] rounded-full animate-pulse" style={{animationDelay: "0ms"}} />
                  <span className="w-1 h-3 bg-[hsl(0,72%,51%)] rounded-full animate-pulse" style={{animationDelay: "150ms"}} />
                  <span className="w-1 h-1.5 bg-[hsl(0,72%,51%)] rounded-full animate-pulse" style={{animationDelay: "300ms"}} />
                  <span className="w-1 h-4 bg-[hsl(0,72%,51%)] rounded-full animate-pulse" style={{animationDelay: "100ms"}} />
                  <span className="w-1 h-2 bg-[hsl(0,72%,51%)] rounded-full animate-pulse" style={{animationDelay: "250ms"}} />
                </span>
                {`Listening · ${(({ browser: "Browser", whisper: "Whisper (GPU)", "nova-sonic": "Nova Sonic" } as Record<string, string>)[cfg.sttEngine]) || cfg.sttEngine}`}
              </span>
            ) : (
              <span className="text-[hsl(0,0%,55%)] italic">Starting...</span>
            )}
          </div>
        ) : (
          <>
            <input value={input} onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && sendMessage()}
              placeholder="Ask me anything about our products..."
              className="flex-1 border border-[hsl(0,0%,90%)] rounded-sm px-4 py-2 text-sm bg-white focus:outline-none focus:border-[hsl(0,0%,60%)] placeholder:text-[hsl(0,0%,65%)]" disabled={loading} />
            <button onClick={() => sendMessage()} disabled={loading}
              className="bg-[hsl(0,0%,12%)] text-white px-6 py-2 rounded-sm text-sm font-medium tracking-wider uppercase hover:bg-[hsl(0,0%,20%)] disabled:opacity-40 transition-colors">
              Send
            </button>
          </>
        )}

        {/* Assistant audio toggle */}
        <button onClick={() => { setAssistantAudio(!assistantAudio); if (assistantAudio) window.speechSynthesis?.cancel(); }}
          className={`px-2 py-2 rounded-sm border transition-colors flex-shrink-0 ${
            assistantAudio
              ? "border-[hsl(0,0%,12%)] text-[hsl(0,0%,12%)]"
              : "border-[hsl(0,0%,85%)] text-[hsl(0,0%,65%)]"
          }`}
          title={assistantAudio ? "Mute Browser TTS" : "Enable Browser TTS (text-to-speech)"}>
          {assistantAudio ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" /><path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" /><line x1="23" y1="9" x2="17" y2="15" /><line x1="17" y1="9" x2="23" y2="15" />
            </svg>
          )}
        </button>
        {cfg.liveAvatarEnabled && avatarSpeaking && (
          <button onClick={interruptAvatar} title="Stop LiveAvatar speaking"
            className="border border-[hsl(0,0%,85%)] text-[hsl(0,0%,40%)] px-3 py-2 rounded-sm text-sm hover:border-[hsl(0,0%,60%)] hover:text-[hsl(0,0%,12%)] transition-colors">
            Stop
          </button>
        )}
      </div>

      {/* Camera capture overlay */}
      {showCamera && (
        <CameraCapture
          onCapture={handleCameraCapture}
          onClose={() => { setShowCamera(false); setPendingTryonProductId(null); }}
          autoCapture={true}
          countdownSeconds={10}
        />
      )}

      {/* Image zoom overlay */}
      {zoomImg && (
        <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center overflow-auto" onClick={() => { if (zoomed) setZoomed(false); else setZoomImg(null); }}>
          <img src={api.tryonImgSrc(zoomImg)} alt="Zoomed" className={`rounded-sm shadow-lg transition-transform duration-200 ${zoomed ? "max-w-none cursor-zoom-out scale-150" : "max-w-[90vw] max-h-[90vh] cursor-zoom-in"}`} onClick={(e) => { e.stopPropagation(); setZoomed(!zoomed); }} />
        </div>
      )}
    </div>
  );
}
