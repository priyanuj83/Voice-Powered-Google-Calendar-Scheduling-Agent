/**
 * Voice Scheduling Assistant — frontend logic.
 * Web Speech API (STT + TTS), chat UI, API calls to backend with logging.
 */

const API_URL = "https://voice-powered-google-calendar-scheduling.onrender.com"; // Change to deployed URL for production

let messages = [];
let isListening = false;
let isSpeaking = false;
let recognition = null;
let bannerHideTimeoutId = null;

const BANNER_FADE_MS = 400;
const BANNER_VISIBLE_MS = 10000;

const chatArea = document.getElementById("chatArea");
const successBanner = document.getElementById("successBanner");
const successBannerText = document.getElementById("successBannerText");
const eventLink = document.getElementById("eventLink");
const micBtn = document.getElementById("micBtn");
const statusEl = document.getElementById("status");

function setStatus(text) {
  if (statusEl) statusEl.textContent = text;
}

function addMessage(role, text) {
  if (!chatArea) return;
  const div = document.createElement("div");
  div.className = `message ${role}`;
  const p = document.createElement("p");
  p.textContent = text;
  div.appendChild(p);
  chatArea.appendChild(div);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function speakText(text) {
  if (!text || !window.speechSynthesis) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.0;
  utterance.pitch = 1.0;
  utterance.onstart = () => {
    isSpeaking = true;
    setStatus("Speaking...");
    console.log("[TTS] start");
  };
  utterance.onend = () => {
    isSpeaking = false;
    setStatus("Click the microphone to speak");
    console.log("[TTS] end");
  };
  speechSynthesis.speak(utterance);
}

function showSuccessBanner(message, showEventLink, eventLinkHref) {
  if (!successBanner) return;
  if (bannerHideTimeoutId) {
    clearTimeout(bannerHideTimeoutId);
    bannerHideTimeoutId = null;
  }
  if (successBannerText) successBannerText.textContent = message;
  if (eventLink) {
    eventLink.style.display = showEventLink ? "" : "none";
    if (eventLinkHref) eventLink.href = eventLinkHref;
  }
  successBanner.style.display = "flex";
  successBanner.offsetHeight;
  successBanner.style.opacity = "1";
  bannerHideTimeoutId = setTimeout(() => {
    bannerHideTimeoutId = null;
    successBanner.style.opacity = "0";
    setTimeout(() => {
      successBanner.style.display = "none";
      successBanner.style.opacity = "1";
    }, BANNER_FADE_MS);
  }, BANNER_VISIBLE_MS);
}

async function sendMessage() {
  console.log("[API] Sending request, message_count=" + messages.length);
  try {
    const res = await fetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    }
    const data = await res.json();
    const reply = data.reply || "";
    const eventCreated = !!data.event_created;
    const eventDeleted = !!data.event_deleted;
    console.log(
      "[API] Response received, event_created=" + eventCreated + ", event_deleted=" + eventDeleted + ", reply_preview=" + (reply.substring(0, 100) || "(empty)")
    );

    messages.push({ role: "assistant", content: reply });
    addMessage("assistant", reply);
    chatArea.scrollTop = chatArea.scrollHeight;

    if (eventCreated && data.event_link) {
      showSuccessBanner("✓ Event created!", true, data.event_link);
    }
    if (eventDeleted) {
      showSuccessBanner("✓ Event removed from calendar.", false);
    }

    speakText(reply);
    setStatus("Click the microphone to speak");
  } catch (err) {
    console.error("[API] Request failed:", err);
    const fallback = "Something went wrong, please try again.";
    messages.push({ role: "assistant", content: fallback });
    addMessage("assistant", fallback);
    setStatus("Click the microphone to speak");
  }
}

function initRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    console.error("[Voice] Speech recognition not supported");
    setStatus("Speech recognition not supported (use Chrome or Edge)");
    if (micBtn) micBtn.disabled = true;
    return;
  }
  recognition = new SpeechRecognition();
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = false;

  recognition.onresult = (event) => {
    const transcript = (event.results[0] && event.results[0][0]) ? event.results[0][0].transcript.trim() : "";
    if (!transcript) {
      console.log("[Voice] Empty transcript, ignoring");
      setStatus("Didn't catch that, try again");
      isListening = false;
      if (micBtn) micBtn.classList.remove("listening");
      return;
    }
    console.log("[Voice] User said:", transcript);
    isListening = false;
    if (micBtn) micBtn.classList.remove("listening");
    setStatus("Thinking...");
    addMessage("user", transcript);
    messages.push({ role: "user", content: transcript });
    sendMessage();
  };

  recognition.onerror = (event) => {
    console.error("[Voice] Recognition error:", event.error);
    isListening = false;
    if (micBtn) micBtn.classList.remove("listening");
    setStatus(event.error === "no-speech" ? "Didn't catch that, try again" : "Error: " + event.error);
  };

  recognition.onend = () => {
    if (isListening) {
      // Stopped without result (e.g. no speech)
      isListening = false;
      if (micBtn) micBtn.classList.remove("listening");
      setStatus("Click the microphone to speak");
    }
  };
}

const DEFAULT_FIRST_MESSAGE = "Hello! 👋 Welcome to your Google Calendar assistant. What's your name?";

async function fetchInitialGreeting() {
  try {
    const initRes = await fetch(`${API_URL}/api/init`);
    if (initRes.ok) {
      const initData = await initRes.json();
      const greeting = initData.greeting || DEFAULT_FIRST_MESSAGE;
      console.log("[API] Initial greeting from /api/init");
      messages = [
        { role: "user", content: "Hello" },
        { role: "assistant", content: greeting },
      ];
      addMessage("assistant", greeting);
      speakText(greeting);
      setStatus("Click the microphone to speak");
      return;
    }
  } catch (err) {
    console.warn("[API] /api/init failed, falling back to /api/chat:", err);
  }
  const initMessages = [{ role: "user", content: "Hello" }];
  console.log("[API] Initial greeting via /api/chat");
  try {
    const res = await fetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: initMessages }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const reply = data.reply || DEFAULT_FIRST_MESSAGE;
    console.log("[API] Initial greeting received, reply_preview=" + reply.substring(0, 80));
    messages = [
      { role: "user", content: "Hello" },
      { role: "assistant", content: reply },
    ];
    addMessage("assistant", reply);
    speakText(reply);
  } catch (err) {
    console.error("[API] Initial greeting failed:", err);
    messages = [
      { role: "user", content: "Hello" },
      { role: "assistant", content: DEFAULT_FIRST_MESSAGE },
    ];
    addMessage("assistant", DEFAULT_FIRST_MESSAGE);
    speakText(DEFAULT_FIRST_MESSAGE);
  }
  setStatus("Click the microphone to speak");
}

function onMicClick() {
  if (isSpeaking) {
    console.log("[Mic] Ignored (TTS active)");
    return;
  }
  if (isListening) {
    console.log("[Mic] Stop listening");
    recognition.stop();
    isListening = false;
    micBtn.classList.remove("listening");
    setStatus("Click the microphone to speak");
    return;
  }
  console.log("[Mic] Start listening");
  recognition.start();
  isListening = true;
  micBtn.classList.add("listening");
  setStatus("Listening...");
}

// ——— Init ———
console.log("[App] Using API:", API_URL);
initRecognition();
fetchInitialGreeting();
if (micBtn) micBtn.addEventListener("click", onMicClick);
