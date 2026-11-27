// Gemini through Firebase AI Logic, hybrid: Gemini Nano in Chrome on this computer first, Google's cloud when it
// cannot run there.
const FIREBASE = "https://www.gstatic.com/firebasejs/13.4.0";
// Firebase web settings are public by design: they name the project, they are not passwords.
const FIREBASE_CONFIG = {
  apiKey: "AIzaSyBE68Jg4f4e1a50jNj0ndzKol5J2kIv_24",
  authDomain: "threadline-409ee.firebaseapp.com",
  projectId: "threadline-409ee",
  storageBucket: "threadline-409ee.firebasestorage.app",
  messagingSenderId: "178444314681",
  appId: "1:178444314681:web:ff3594cb2ef59e4954c5e8",
};
// App Check proves cloud requests come from Threadline's page. Public site key (reCAPTCHA Enterprise).
const RECAPTCHA_SITE_KEY = "6LftqDIuAAAAAHJpN5O9GeiasqW5QAowVlewDOpW";
const CLOUD_MODEL = "gemini-3.5-flash-lite";
export const LOCAL = ["localhost", "127.0.0.1"].includes(location.hostname);
const TEXT = [{ type: "text", languages: ["en"] }];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child != null && child !== false) node.append(child instanceof Node ? child : String(child));
  return node;
}

// Turn Google's and Chrome's errors into something a person can act on.
export function explain(error) {
  const text = String(error?.message || error);
  if (/app-check|appcheck|App Check/i.test(text)) return "Google couldn't confirm this request came from Threadline, so the cloud refused it.";
  if (/403|PERMISSION_DENIED|Permission denied/i.test(text)) return "Google refused the cloud request.";
  if (/429|RESOURCE_EXHAUSTED|quota/i.test(text)) return "The free cloud allowance is used up for now. Try again later.";
  if (/on-device|LanguageModel|not available|unavailable/i.test(text)) return "Gemini Nano isn't available on this computer.";
  if (/Failed to fetch|NetworkError|Load failed/i.test(text)) return "Couldn't reach Google. Check your connection.";
  return text;
}

const AI = { firebase: null, models: {} };

async function loadFirebase() {
  if (AI.firebase) return AI.firebase;
  const [app, ai, check] = await Promise.all([
    import(`${FIREBASE}/firebase-app.js`), import(`${FIREBASE}/firebase-ai.js`), import(`${FIREBASE}/firebase-app-check.js`)]);
  const firebaseApp = app.initializeApp(FIREBASE_CONFIG);
  if (LOCAL) self.FIREBASE_APPCHECK_DEBUG_TOKEN = localStorage.getItem("threadline-debug-token") || true;
  check.initializeAppCheck(firebaseApp, { provider: new check.ReCaptchaEnterpriseProvider(RECAPTCHA_SITE_KEY), isTokenAutoRefreshEnabled: true });
  AI.firebase = { ai, backend: ai.getAI(firebaseApp, { backend: new ai.GoogleAIBackend() }) };
  return AI.firebase;
}

// With a JSON schema, both Gemini Nano and the cloud model can only answer in that shape.
async function model(mode = "PREFER_ON_DEVICE", schema = null) {
  const { ai, backend } = await loadFirebase();
  const key = mode + (schema ? JSON.stringify(schema) : "");
  AI.models[key] ||= ai.getGenerativeModel(backend, {
    mode: ai.InferenceMode[mode],
    onDeviceParams: { createOptions: { expectedInputs: TEXT, expectedOutputs: TEXT }, ...(schema && { promptOptions: { responseConstraint: schema } }) },
    inCloudParams: { model: CLOUD_MODEL, generationConfig: { temperature: 0, ...(schema && { responseMimeType: "application/json", responseJsonSchema: schema }) } },
  });
  return AI.models[key];
}

// "available", "downloadable", "downloading", "unavailable", or "missing" (no Gemini Nano in this browser).
export async function deviceState() {
  if (!("LanguageModel" in self)) return "missing";
  try { return await LanguageModel.availability({ expectedInputs: TEXT, expectedOutputs: TEXT }); } catch { return "unavailable"; }
}

export async function downloadNano(onProgress) {
  const m = await model();
  await m.initializeDeviceModel((fraction) => onProgress?.(fraction));
}

const listeners = new Set();
export function onAnswer(listener) { listeners.add(listener); }

// Asks Gemini and hands over the text as it is written, so it can appear word by word.
export async function askStream(prompt, onText, mode, schema) {
  const started = performance.now();
  const m = await model(mode || undefined, schema);
  const result = await m.generateContentStream(prompt);
  let text = "";
  for await (const chunk of result.stream) {
    text += chunk.text();
    onText(text);
  }
  const response = await result.response;
  const onDevice = response.inferenceSource === AI.firebase.ai.InferenceSource.ON_DEVICE;
  const answer = { text, onDevice, ms: performance.now() - started };
  listeners.forEach((listener) => listener(answer));
  return answer;
}

// Load Gemini Nano into memory while the page opens, so the first answer doesn't wait for it. Only when it's
// already downloaded: this never starts a download.
let warm = null;
export async function warmUp() {
  if (warm || (await deviceState()) !== "available") return;
  try { warm = await LanguageModel.create({ expectedInputs: TEXT, expectedOutputs: TEXT }); } catch { /* The first answer loads it instead. */ }
}

// Text that looks like it holds a password, key or private address. Threadline already blanks known key formats
// when it saves your history ([REDACTED]); where it did, the text around it is sensitive too. Such text is only
// read by Gemini Nano on this computer, never sent to the cloud.
const PRIVATE = [
  /\[REDACTED/i, /-----BEGIN [A-Z ]*PRIVATE KEY/, /\b(?:password|passwd|passphrase|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]/i,
  /^\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}=(?=\S*\d)\S{12,}\s*$/m,  // .env lines with a key-like value
  /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp):\/\/[^\s/]*@/i,  // connection strings with a user
];
export const looksPrivate = (text) => PRIVATE.some((pattern) => pattern.test(String(text || "")));
