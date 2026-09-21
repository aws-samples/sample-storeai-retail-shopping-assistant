"use client";
import { useState, useRef, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import { useConfig, type ChatModel, type STTEngine } from "@/context/ConfigContext";
import { useCognito } from "@/context/CognitoAuthContext";
import { createAvatarWindow } from "@/lib/avatar";

// Tap/click-toggle info popover — works on touchscreens (booth kiosk) AND mouse,
// unlike a native `title` tooltip which only appears on hover.
function InfoTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-block align-super">
      <button
        type="button"
        aria-label="More info"
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        className="text-[9px] text-[hsl(0,0%,55%)] hover:text-[hsl(0,0%,12%)] cursor-pointer leading-none"
      >
        ⓘ
      </button>
      {open && (
        <>
          <span className="fixed inset-0 z-40" onClick={(e) => { e.stopPropagation(); setOpen(false); }} />
          <span
            className="absolute z-50 left-0 top-5 w-56 rounded-md bg-[hsl(0,0%,12%)] text-white text-[11px] leading-snug p-2 shadow-lg normal-case tracking-normal font-normal"
            onClick={(e) => e.stopPropagation()}
          >
            {text}
          </span>
        </>
      )}
    </span>
  );
}

const ALL_NAV = [
  { href: "/shop", label: "Shop", badgeKey: "" },
  { href: "/cart", label: "Cart", badgeKey: "cart" },
  { href: "/tryon-room", label: "Try-On Room", badgeKey: "tryon" },
  { href: "/assistant", label: "Assistant", badgeKey: "" },
];

const BOOTH_SHOPPING_PAGES = ["/shop", "/cart", "/tryon-room", "/assistant"];
const BOOTH_TRYON_PAGES = ["/tryon-room", "/cart", "/assistant"];

const HAS_LIVEAVATAR = !!process.env.NEXT_PUBLIC_NOVA_SONIC_HOST || !!process.env.NEXT_PUBLIC_LIVEAVATAR_API_KEY || process.env.NEXT_PUBLIC_LIVEAVATAR_ENABLED === "true";

function Toggle({ label, value, onChange, help }: { label: string; value: boolean; onChange: (v: boolean) => void; help?: string }) {
  return (
    <label className="flex items-center justify-between text-xs cursor-pointer group" title={help}>
      <span className="text-[hsl(0,0%,30%)] group-hover:text-[hsl(0,0%,12%)]">{label}</span>
      <div className={`w-8 h-[18px] rounded-full relative transition-colors ${value ? "bg-[hsl(0,0%,12%)]" : "bg-[hsl(0,0%,82%)]"}`} onClick={() => onChange(!value)}>
        <div className={`absolute top-[2px] w-[14px] h-[14px] bg-white rounded-full shadow-sm transition-transform ${value ? "translate-x-[14px]" : "translate-x-[2px]"}`} />
      </div>
    </label>
  );
}

export default function Sidebar() {
  const { idToken } = useCognito();
  const tokenRef = useRef(idToken);
  tokenRef.current = idToken;
  const avatarWindow = useRef<ReturnType<typeof createAvatarWindow> | null>(null);
  const [avatarError, setAvatarError] = useState("");
  useEffect(() => () => { avatarWindow.current?.dispose(); }, []);
  useEffect(() => {
    if (!idToken && process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID) avatarWindow.current?.close();
  }, [idToken]);
  const openAvatar = () => {
    try {
      avatarWindow.current ??= createAvatarWindow(() => tokenRef.current, setAvatarError);
      avatarWindow.current.open();
    } catch (error) {
      setAvatarError(error instanceof Error ? error.message : "Unable to open the avatar.");
    }
  };
  const path = usePathname();
  const { customer, signOut, cartCount, tryonCount } = useAuth();
  const cfg = useConfig();
  const isAssistant = path === "/assistant";
  const showVton = isAssistant || path === "/tryon-room";
  const vtonDisabled = cfg.demoMode === "booth" && cfg.boothStation === "shopping";
  const [moreOpen, setMoreOpen] = useState(false);

  const NAV = cfg.demoMode === "booth"
    ? ALL_NAV.filter((n) => (cfg.boothStation === "shopping" ? BOOTH_SHOPPING_PAGES : BOOTH_TRYON_PAGES).includes(n.href))
    : ALL_NAV;

  return (
    <>
      <button onClick={() => cfg.setSidebarOpen(!cfg.sidebarOpen)}
        className="fixed top-4 left-3 z-50 text-[hsl(0,0%,60%)] hover:text-[hsl(0,0%,12%)] transition-colors w-6 h-6 flex items-center justify-center">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
          <line x1="2" y1="4" x2="14" y2="4" /><line x1="2" y1="8" x2="14" y2="8" /><line x1="2" y1="12" x2="14" y2="12" />
        </svg>
      </button>

      {cfg.sidebarOpen && (
        <aside className="w-56 border-r border-[hsl(0,0%,92%)] bg-white h-screen flex flex-col p-5 pt-12 fixed z-40 overflow-y-auto">
          <div className="text-base font-semibold tracking-tight text-[hsl(0,0%,12%)] mb-5">StoreAI</div>

          <nav className="flex flex-col gap-0.5">
            {NAV.map((n) => {
              const count = n.badgeKey === "cart" ? cartCount : n.badgeKey === "tryon" ? tryonCount : 0;
              const active = path === n.href;

              return (
                <Link key={n.href} href={n.href}
                  className={`px-3 py-2 rounded text-sm transition-colors flex items-center justify-between ${
                    active ? "bg-[hsl(40,10%,96%)] font-medium text-[hsl(0,0%,12%)]" : "text-[hsl(0,0%,30%)] hover:bg-[hsl(40,10%,97%)] hover:text-[hsl(0,0%,12%)]"
                  }`}>
                  <span>{n.label}</span>
                  {count > 0 && (
                    <span className="bg-[hsl(0,0%,12%)] text-white text-[10px] font-bold rounded-full h-4 min-w-[16px] flex items-center justify-center px-1">
                      {count}
                    </span>
                  )}
                </Link>
              );
            })}
          </nav>

          <div className="border-t border-[hsl(0,0%,92%)] my-4" />

          {customer ? (
            <div className="text-sm">
              <div className="font-medium text-[hsl(0,0%,12%)]">{customer.first_name} {customer.last_name}</div>
              <div className="text-[11px] text-[hsl(0,0%,55%)] mt-0.5">{customer.customer_id}</div>
              <button onClick={() => { signOut(); cfg.resetCost(); }} className="mt-2 text-[11px] text-[hsl(0,0%,55%)] hover:text-[hsl(0,72%,51%)] transition-colors">Sign out</button>
            </div>
          ) : (
            <div className="text-sm text-[hsl(0,0%,55%)]">
              <div className="font-medium text-[hsl(0,0%,30%)]">Guest</div>
              <div className="text-[11px] mt-0.5">Sign in via Assistant</div>
            </div>
          )}

          {/* Session Cost */}
          {(cfg.sessionCost.llm > 0 || cfg.sessionCost.vton > 0 || cfg.selectionCost > 0) && (
            <>
              <div className="border-t border-[hsl(0,0%,92%)] my-4" />
              <div className="flex items-center justify-between mb-2">
                <span className="text-[10px] uppercase tracking-[2px] text-[hsl(0,0%,55%)] font-medium">Session Cost <InfoTip text="Total estimated cost for this customer session — AI model inference plus a rough AWS infrastructure estimate. Resets when the customer signs out." /></span>
                <span className="text-xs font-semibold text-[hsl(0,0%,12%)]">${(cfg.sessionCost.llm + cfg.sessionCost.vton + cfg.sessionCost.stt + cfg.sessionCost.infra + cfg.selectionCost).toFixed(4)}</span>
              </div>
              <div className="flex flex-col gap-1 text-[11px] text-[hsl(0,0%,45%)]">
                {cfg.selectionCost > 0 && cfg.demoMode === "booth" && cfg.boothStation === "tryon" && (
                  <div className="flex justify-between"><span>Selection <InfoTip text="Cost incurred during the Selection station — browsing, product search, and cart management." /></span><span>${cfg.selectionCost.toFixed(4)}</span></div>
                )}
                {cfg.sessionCost.llm > 0 && <div className="flex justify-between"><span>LLM <InfoTip text="LLM inference, routed through the LiteLLM gateway to Amazon Bedrock. Claude Sonnet 4.6 is billed per token: $3.00 per 1M input tokens and $15.00 per 1M output tokens. Cost = (input×$3 + output×$15) ÷ 1,000,000, summed over all calls this session." /></span><span>${cfg.sessionCost.llm.toFixed(4)} ({cfg.sessionCost.llmCalls})</span></div>}
                {cfg.sessionCost.vton > 0 && <div className="flex justify-between"><span>VTON <InfoTip text="Virtual try-on image generation — FASHN (GPU) or Qwen-Image-Edit on Neuron, with cost amortized over inference time. Only appears when a VTON engine is deployed." /></span><span>${cfg.sessionCost.vton.toFixed(4)} ({cfg.sessionCost.vtonImages})</span></div>}
                {cfg.sessionCost.stt > 0 && <div className="flex justify-between"><span>STT <InfoTip text="Speech-to-text — Whisper (GPU) amortized per second, or Nova Sonic per-token. Only appears when voice is deployed." /></span><span>${cfg.sessionCost.stt.toFixed(4)} ({cfg.sessionCost.sttSec.toFixed(0)}s)</span></div>}
                {cfg.sessionCost.infra > 0 && <div className="flex justify-between"><span>Infra <InfoTip text="Rough estimate of AWS infrastructure per turn — Lambda (MCP tool) invocations and DynamoDB reads plus per-request overhead. An approximation, typically well under $0.01 per session." /></span><span>{"<"} $0.01</span></div>}
              </div>
            </>
          )}

          <div className="border-t border-[hsl(0,0%,92%)] my-4" />
          <div className="text-[10px] uppercase tracking-[2px] text-[hsl(0,0%,55%)] font-medium mb-3">Settings</div>

          <label className="text-[11px] text-[hsl(0,0%,45%)] mb-1">Chat Model</label>
          <select value={cfg.chatModel} onChange={(e) => cfg.setChatModel(e.target.value as ChatModel)}
            className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1.5 bg-white mb-2">
            <option value="claude-haiku-4.5">Claude Haiku 4.5</option>
            <option value="claude-sonnet-4.6">Claude Sonnet 4.6</option>
            <option value="claude-opus-4.1">Claude Opus 4.1</option>
            <option value="nova-pro">Amazon Nova Pro</option>
            <option value="qwen3">Qwen3 8B (Self-Hosted)</option>
          </select>

          {showVton && (
            <>
              <label className={`text-[11px] mb-1 ${vtonDisabled ? "text-[hsl(0,0%,75%)]" : "text-[hsl(0,0%,45%)]"}`}>Try-On Engine</label>
              <select value={cfg.vtonEngine} onChange={(e) => cfg.setVtonEngine(e.target.value as any)}
                disabled={vtonDisabled}
                className={`text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1.5 bg-white mb-2 ${vtonDisabled ? "opacity-50 cursor-not-allowed" : ""}`}>
                <option value="qwen_image_edit">Qwen Image Edit (Neuron)</option>
                <option value="fashn_vton">FASHN VTON v1.5 (GPU)</option>
              </select>
              {!vtonDisabled && cfg.vtonEngine === "qwen_image_edit" && (
                <div className="flex flex-col gap-1.5 ml-1 mb-2">
                  <label className="text-[10px] text-[hsl(0,0%,55%)]">Steps: {cfg.vtonSteps}</label>
                  <input type="range" min={10} max={100} value={cfg.vtonSteps}
                    onChange={(e) => cfg.setVtonSteps(Number(e.target.value))} className="w-full h-1 accent-[hsl(0,0%,12%)]" />
                  <label className="text-[10px] text-[hsl(0,0%,55%)]">CFG: {cfg.vtonCfgScale}</label>
                  <input type="range" min={0.5} max={10} step={0.5} value={cfg.vtonCfgScale}
                    onChange={(e) => cfg.setVtonCfgScale(Number(e.target.value))} className="w-full h-1 accent-[hsl(0,0%,12%)]" />
                  <label className="text-[10px] text-[hsl(0,0%,55%)]">Seed: {cfg.vtonSeed}</label>
                  <input type="number" min={0} max={9999} value={cfg.vtonSeed}
                    onChange={(e) => cfg.setVtonSeed(Number(e.target.value))}
                    className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-0.5 w-20" />
                </div>
              )}

            </>
          )}

          {isAssistant && (
            <>
              <label className="text-[11px] text-[hsl(0,0%,45%)] mb-1">STT Engine</label>
              <select value={cfg.sttEngine} onChange={(e) => cfg.setSttEngine(e.target.value as STTEngine)}
                className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1.5 bg-white mb-2">
                <option value="browser">Browser (Web Speech)</option>
                <option value="whisper">Whisper (Self-Hosted)</option>
                <option value="nova-sonic">Nova Sonic</option>
              </select>
              {HAS_LIVEAVATAR && (
                <div className="mb-2">
                  <Toggle label="AI Avatar (TTS)" value={cfg.liveAvatarEnabled} onChange={cfg.setLiveAvatarEnabled} />
                  {cfg.liveAvatarEnabled && (
                    <button type="button" onClick={openAvatar}
                      className="ml-4 mt-1 text-[11px] text-[hsl(0,0%,12%)] underline underline-offset-2 hover:text-[hsl(0,0%,30%)]">
                      Open Avatar Window
                    </button>
                  )}
                  {avatarError && <p role="alert" className="text-xs text-red-700">{avatarError}</p>}
                </div>
              )}
            </>
          )}

          {/* Booth station selector */}
          {cfg.demoMode === "booth" && (
            <>
              <div className="border-t border-[hsl(0,0%,92%)] my-3" />
              <label className="text-[11px] text-[hsl(0,0%,45%)] mb-1">Station</label>
              <div className="flex gap-1.5 text-xs">
                {(["shopping", "tryon"] as const).map((s) => (
                  <button key={s} onClick={() => cfg.setBoothStation(s)}
                    className={`px-2.5 py-1 rounded transition-colors ${cfg.boothStation === s ? "bg-[hsl(0,0%,12%)] text-white" : "border border-[hsl(0,0%,90%)] hover:bg-[hsl(40,10%,96%)]"}`}>
                    {s === "shopping" ? "Selection" : "Try-On"}
                  </button>
                ))}
              </div>
            </>
          )}

          {/* Spacer to push "More" to bottom */}
          <div className="flex-1" />

          {/* Collapsible "More Options" — rarely used toggles */}
          {isAssistant && (
            <div className="border-t border-[hsl(0,0%,92%)] pt-3">
              <button onClick={() => setMoreOpen(!moreOpen)}
                className="flex items-center justify-between w-full text-[10px] uppercase tracking-[2px] text-[hsl(0,0%,55%)] font-medium hover:text-[hsl(0,0%,30%)] transition-colors">
                <span>More Options</span>
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5"
                  className={`transition-transform ${moreOpen ? "rotate-180" : ""}`}>
                  <path d="M3 4.5L6 7.5L9 4.5" />
                </svg>
              </button>

              {moreOpen && (
                <div className="mt-3 flex flex-col gap-2">
                  <Toggle label="Voice (Nova Sonic)" value={cfg.voiceMode} onChange={cfg.setVoiceMode}
                    help="Real-time speech-to-speech via Amazon Nova Sonic" />
                  {cfg.voiceMode && (
                    <select value={cfg.novaSonicVoice} onChange={(e) => cfg.setNovaSonicVoice(e.target.value)}
                      className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1 ml-4">
                      <option value="matthew">Matthew</option>
                      <option value="tiffany">Tiffany</option>
                      <option value="ruth">Ruth</option>
                    </select>
                  )}

                  <Toggle label="Inline Try-On" value={cfg.inlineTryon} onChange={cfg.setInlineTryon} />
                  <Toggle label="Save Chat" value={cfg.saveChat} onChange={cfg.setSaveChat} />
                  <Toggle label="Photo Upload" value={cfg.photoUploadEnabled} onChange={cfg.setPhotoUploadEnabled} />
                  <Toggle label="Debug Mode" value={cfg.debugMode} onChange={cfg.setDebugMode} />
                  {cfg.demoMode === "booth" && (
                    <>
                      <label className="text-[11px] text-[hsl(0,0%,45%)] mt-1 mb-0.5">Max Items <span className="text-[9px] text-[hsl(0,0%,60%)]">(Cart + Try-On Room)</span></label>
                      <input type="number" min={1} max={20} value={cfg.boothItemCap}
                        onChange={(e) => cfg.setBoothItemCap(Math.max(1, Math.min(20, Number(e.target.value))))}
                        className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1.5 w-16 bg-white" />
                    </>
                  )}
                  {cfg.demoMode === "booth" && cfg.boothStation === "tryon" && (
                    <Toggle label="Auto Queue Sign-In" value={cfg.autoQueueSignIn} onChange={cfg.setAutoQueueSignIn}
                      help="Automatically sign in the next customer from the queue" />
                  )}
                  {cfg.demoMode === "booth" && (
                    <>
                      <label className="text-[11px] text-[hsl(0,0%,45%)] mt-2 mb-0.5">Marker Color</label>
                      <select value={cfg.markerColor} onChange={(e) => cfg.setMarkerColor(e.target.value as any)}
                        className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1 bg-white">
                        <option value="red">Red</option>
                        <option value="blue">Blue</option>
                        <option value="green">Green</option>
                      </select>
                      <label className="text-[11px] text-[hsl(0,0%,45%)] mt-1 mb-0.5">Marker Height (cm)</label>
                      <input type="number" min={10} max={300} value={cfg.markerHeight}
                        onChange={(e) => cfg.setMarkerHeight(Math.max(10, Math.min(300, Number(e.target.value))))}
                        className="text-xs border border-[hsl(0,0%,90%)] rounded px-2 py-1.5 w-20 bg-white" />
                    </>
                  )}
                </div>
              )}
            </div>
          )}
        </aside>
      )}
    </>
  );
}
