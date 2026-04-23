import { startTransition, useEffect, useMemo, useRef, useState } from "react";

const starterPrompts = [
  "Why is the cart service down?",
  "What pods are not running in default?",
  "Show memory-related issues from the last hour.",
];

const BACKEND_BASE_URL = "http://127.0.0.1:8000";
const sidebarNavItems = [
  { id: "new-chat", label: "New chat", icon: "new-chat" },
  { id: "search", label: "Search chats", icon: "search" },
];

function createSessionId() {
  return `session-${Math.random().toString(36).slice(2, 10)}`;
}

function formatMessageTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatSessionTitle(value) {
  if (!value) {
    return "Untitled chat";
  }
  const cleaned = value.replace(/\s+/g, " ").trim();
  if (cleaned.length <= 44) {
    return cleaned;
  }
  return `${cleaned.slice(0, 41)}...`;
}

function deriveChatTitle(value) {
  const cleaned = (value || "").replace(/\s+/g, " ").trim();
  if (!cleaned) {
    return "Untitled chat";
  }

  const lowered = cleaned.toLowerCase();
  const prefixes = [
    "what is ",
    "what are ",
    "how does ",
    "how do i ",
    "show me ",
    "can you ",
    "please ",
    "why is ",
    "why does ",
    "tell me about ",
    "explain ",
  ];

  let title = cleaned;
  for (const prefix of prefixes) {
    if (lowered.startsWith(prefix)) {
      title = cleaned.slice(prefix.length).trim();
      break;
    }
  }

  title = title.replace(/[?.!]+$/g, "").trim();
  const words = title.split(" ").filter(Boolean);
  if (!words.length) {
    return "Untitled chat";
  }
  return formatSessionTitle(`${words.slice(0, 7).join(" ").charAt(0).toUpperCase()}${words.slice(0, 7).join(" ").slice(1)}`);
}

function formatSessionPreview(value) {
  if (!value) {
    return "Open this conversation again.";
  }
  const cleaned = value.replace(/\s+/g, " ").trim();
  if (cleaned.length <= 68) {
    return cleaned;
  }
  return `${cleaned.slice(0, 65)}...`;
}

function parseStructuredAssistantText(text) {
  if (!text || !text.includes("## ")) {
    return null;
  }

  const sections = {};
  let active = "";
  for (const line of text.split("\n")) {
    const heading = line.match(/^##\s+(.+)$/);
    if (heading) {
      active = heading[1].trim();
      sections[active] = [];
      continue;
    }
    if (active) {
      sections[active].push(line);
    }
  }

  if (!Object.keys(sections).length) {
    return null;
  }

  const toolCalls = (sections["Tool Calls"] || [])
    .map((line) => line.trim())
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).trim());

  const severityLines = (sections["Severity"] || []).map((line) => line.trim()).filter(Boolean);
  const severity = severityLines[0] || "Investigating";

  const findings = (sections["Findings"] || sections["Evidence"] || [])
    .map((line) => line.trim())
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).trim());

  const rootCause = (sections["Root Cause"] || [])
    .join("\n")
    .replace(/```[\s\S]*?```/g, "")
    .trim();

  const fixBlock = (sections["Fix"] || sections["Suggested fix"] || []).join("\n");
  const fix = fixBlock.replace(/```bash|```/g, "").trim();

  const nextQuestions = (sections["Follow-ups"] || sections["Next Questions"] || [])
    .map((line) => line.trim())
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).trim());

  return {
    toolCalls,
    severity,
    findings,
    rootCause,
    fix,
    nextQuestions,
  };
}

const assistantSectionLabels = new Set([
  "Issue",
  "Severity",
  "Cluster evidence summary",
  "Likely causes",
  "Verify with",
  "Root cause",
  "Suggested fixes",
  "Analysis",
  "Debug",
  "Fix",
  "Summary",
  "Observed",
  "Follow-ups",
  "What is it?",
  "The simple analogy",
  "How it works",
  "Real example",
  "When to use it",
  "Common mistakes",
  "Related topics to learn next",
]);

function stripInlineMarkdown(text) {
  return (text || "").replace(/\*\*(.*?)\*\*/g, "$1").replace(/\*(.*?)\*/g, "$1").trim();
}

function renderInlineContent(text, keyPrefix) {
  const normalized = stripInlineMarkdown(text);
  return normalized.split(/(`[^`]+`)/g).filter(Boolean).map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`")) {
      return <code key={`${keyPrefix}-code-${index}`}>{part.slice(1, -1)}</code>;
    }
    return <span key={`${keyPrefix}-text-${index}`}>{part}</span>;
  });
}

function detectAssistantHeading(line) {
  const normalized = stripInlineMarkdown(line).replace(/:$/, "");
  if (!normalized || normalized === "---") {
    return null;
  }
  if (normalized.startsWith("📘 ")) {
    return { type: "title", text: normalized.replace(/^📘\s*/, "") };
  }
  if (assistantSectionLabels.has(normalized)) {
    return { type: "heading", text: normalized };
  }
  return null;
}

function parseAssistantBlocks(text) {
  const lines = (text || "").split(/\r?\n/);
  const blocks = [];
  let index = 0;

  while (index < lines.length) {
    const rawLine = lines[index] || "";
    const trimmed = rawLine.trim();

    if (!trimmed || trimmed === "---") {
      index += 1;
      continue;
    }

    if (trimmed.startsWith("```")) {
      const language = trimmed.replace(/```/, "").trim();
      index += 1;
      const codeLines = [];
      while (index < lines.length && !lines[index].trim().startsWith("```")) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) {
        index += 1;
      }
      blocks.push({ type: "code", language, text: codeLines.join("\n") });
      continue;
    }

    const severityMatch = stripInlineMarkdown(trimmed).match(/^Severity:\s*(.+)$/i);
    if (severityMatch) {
      blocks.push({ type: "meta", label: "Severity", value: severityMatch[1] });
      index += 1;
      continue;
    }

    const heading = detectAssistantHeading(trimmed);
    if (heading) {
      blocks.push(heading);
      index += 1;
      continue;
    }

    if (trimmed.startsWith("- ")) {
      const items = [];
      while (index < lines.length && lines[index].trim().startsWith("- ")) {
        items.push(stripInlineMarkdown(lines[index].trim().slice(2)));
        index += 1;
      }
      blocks.push({ type: "list", items });
      continue;
    }

    const paragraphLines = [stripInlineMarkdown(trimmed)];
    index += 1;
    while (index < lines.length) {
      const nextTrimmed = lines[index].trim();
      if (!nextTrimmed || nextTrimmed === "---" || nextTrimmed.startsWith("```") || nextTrimmed.startsWith("- ")) {
        break;
      }
      if (stripInlineMarkdown(nextTrimmed).match(/^Severity:\s*(.+)$/i) || detectAssistantHeading(nextTrimmed)) {
        break;
      }
      paragraphLines.push(stripInlineMarkdown(nextTrimmed));
      index += 1;
    }
    blocks.push({ type: "paragraph", text: paragraphLines.join(" ") });
  }

  return blocks;
}

function stripForSpeech(text) {
  return (text || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`/g, "")
    .replace(/\*\*/g, "")
    .replace(/\*/g, "")
    .replace(/\n+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function renderAssistantBlocks(blocks, keyPrefix) {
  return blocks.map((block, index) => {
    const key = `${keyPrefix}-${block.type}-${index}`;

    if (block.type === "title") {
      return <h2 className="assistant-title" key={key}>{renderInlineContent(block.text, key)}</h2>;
    }
    if (block.type === "heading") {
      return <h3 className="assistant-heading" key={key}>{renderInlineContent(block.text, key)}</h3>;
    }
    if (block.type === "meta") {
      return (
        <div className="assistant-meta" key={key}>
          <span className="assistant-meta-label">{block.label}</span>
          <strong className="assistant-meta-value">{block.value}</strong>
        </div>
      );
    }
    if (block.type === "list") {
      return (
        <ul className="assistant-list" key={key}>
          {block.items.map((item, itemIndex) => (
            <li key={`${key}-item-${itemIndex}`}>{renderInlineContent(item, `${key}-item-${itemIndex}`)}</li>
          ))}
        </ul>
      );
    }
    if (block.type === "code") {
      return (
        <pre className="assistant-code" key={key}>
          <code>{block.text}</code>
        </pre>
      );
    }
    return <p className="assistant-paragraph" key={key}>{renderInlineContent(block.text, key)}</p>;
  });
}

function Icon({ name }) {
  const props = {
    width: 18,
    height: 18,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "1.9",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true",
  };

  if (name === "logo") {
    return (
      <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
        <path
          d="M12 1.7l8.9 5.15v10.3L12 22.3 3.1 17.15V6.85z"
          fill="none"
          stroke="#4c54ff"
          strokeWidth="1.1"
          opacity="0.45"
        />
        <path
          d="M12 4.7l6.2 3.58v7.44L12 19.3l-6.2-3.58V8.28z"
          fill="#6f74ff"
          fillOpacity="0.2"
          stroke="#6f74ff"
          strokeWidth="1.35"
        />
        <path d="M12 4.7v14.6" stroke="#8c91ff" strokeWidth="1.25" strokeLinecap="round" />
        <path d="M5.8 8.28L12 12l6.2-3.72" stroke="#8c91ff" strokeWidth="1.25" strokeLinecap="round" />
        <circle cx="12" cy="4.7" r="1" fill="#6f74ff" />
        <circle cx="18.2" cy="8.28" r="1" fill="#6f74ff" />
        <circle cx="18.2" cy="15.72" r="1" fill="#6f74ff" />
        <circle cx="12" cy="19.3" r="1" fill="#6f74ff" />
        <circle cx="5.8" cy="15.72" r="1" fill="#6f74ff" />
        <circle cx="5.8" cy="8.28" r="1" fill="#6f74ff" />
        <circle cx="12" cy="12" r="2" fill="#7f83ff" />
        <circle cx="12" cy="12" r="0.9" fill="#f5f7ff" />
      </svg>
    );
  }
  if (name === "panel") {
    return (
      <svg {...props}>
        <rect x="3" y="4" width="18" height="16" rx="3" />
        <path d="M9 4v16" />
        <path d="M14 12h5" />
        <path d="M17 9l3 3-3 3" />
      </svg>
    );
  }
  if (name === "new-chat") {
    return (
      <svg {...props}>
        <path d="M12 5v14" />
        <path d="M5 12h14" />
      </svg>
    );
  }
  if (name === "search") {
    return (
      <svg {...props}>
        <circle cx="11" cy="11" r="6" />
        <path d="M20 20l-3.5-3.5" />
      </svg>
    );
  }
  if (name === "group") {
    return (
      <svg {...props}>
        <circle cx="10" cy="8" r="3.2" />
        <path d="M4.5 20a5.5 5.5 0 0 1 11 0" />
        <path d="M18 8v6" />
        <path d="M15 11h6" />
      </svg>
    );
  }
  if (name === "more") {
    return (
      <svg {...props}>
        <circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none" />
        <circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none" />
      </svg>
    );
  }
  if (name === "chat") {
    return (
      <svg {...props}>
        <path d="M21 15a3 3 0 0 1-3 3H8l-5 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3z" />
      </svg>
    );
  }
  if (name === "archive") {
    return (
      <svg {...props}>
        <rect x="3" y="4" width="18" height="5" rx="2" />
        <path d="M5 9h14v9a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2z" />
        <path d="M10 13h4" />
      </svg>
    );
  }
  if (name === "library") {
    return (
      <svg {...props}>
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
        <path d="M6.5 17A2.5 2.5 0 0 0 4 14.5V5a2 2 0 0 1 2-2h14v14" />
      </svg>
    );
  }
  if (name === "caret") {
    return (
      <svg {...props}>
        <path d="M7 10l5 5 5-5" />
      </svg>
    );
  }
  if (name === "menu") {
    return (
      <svg {...props}>
        <path d="M4 7h16" />
        <path d="M4 12h16" />
        <path d="M4 17h16" />
      </svg>
    );
  }
  if (name === "account") {
    return (
      <svg {...props}>
        <path d="M20 21a8 8 0 0 0-16 0" />
        <circle cx="12" cy="8" r="4" />
      </svg>
    );
  }
  if (name === "theme-dark") {
    return (
      <svg {...props}>
        <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
      </svg>
    );
  }
  if (name === "theme-light") {
    return (
      <svg {...props}>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2.5" />
        <path d="M12 19.5V22" />
        <path d="M4.93 4.93l1.77 1.77" />
        <path d="M17.3 17.3l1.77 1.77" />
        <path d="M2 12h2.5" />
        <path d="M19.5 12H22" />
        <path d="M4.93 19.07l1.77-1.77" />
        <path d="M17.3 6.7l1.77-1.77" />
      </svg>
    );
  }
  if (name === "sparkle") {
    return (
      <svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
        <defs>
          <linearGradient id="kuberonHeroFill" x1="5" y1="5" x2="19" y2="19" gradientUnits="userSpaceOnUse">
            <stop offset="0" stopColor="#7f86ff" />
            <stop offset="1" stopColor="#5e63ff" />
          </linearGradient>
        </defs>
        <path
          d="M12 1.7l8.9 5.15v10.3L12 22.3 3.1 17.15V6.85z"
          fill="none"
          stroke="#6f74ff"
          strokeOpacity="0.55"
          strokeWidth="1.05"
        />
        <path
          d="M12 4.7l6.2 3.58v7.44L12 19.3l-6.2-3.58V8.28z"
          fill="url(#kuberonHeroFill)"
          fillOpacity="0.96"
          stroke="#9ea6ff"
          strokeOpacity="0.96"
          strokeWidth="1.2"
        />
        <path d="M12 4.7v14.6" stroke="#dce0ff" strokeOpacity="0.92" strokeWidth="1.15" strokeLinecap="round" />
        <path d="M5.8 8.28L12 12l6.2-3.72" stroke="#dce0ff" strokeOpacity="0.92" strokeWidth="1.15" strokeLinecap="round" />
        <circle cx="12" cy="4.7" r="0.95" fill="#dce0ff" />
        <circle cx="18.2" cy="8.28" r="0.95" fill="#dce0ff" />
        <circle cx="18.2" cy="15.72" r="0.95" fill="#dce0ff" />
        <circle cx="12" cy="19.3" r="0.95" fill="#dce0ff" />
        <circle cx="5.8" cy="15.72" r="0.95" fill="#dce0ff" />
        <circle cx="5.8" cy="8.28" r="0.95" fill="#dce0ff" />
        <circle cx="12" cy="12" r="1.9" fill="#eef2ff" />
        <circle cx="12" cy="12" r="0.6" fill="#6f74ff" />
      </svg>
    );
  }
  if (name === "attach") {
    return (
      <svg {...props}>
        <path d="M21.44 11.05l-8.49 8.49a5 5 0 0 1-7.07-7.07l9.19-9.2a3.33 3.33 0 0 1 4.71 4.72l-9.2 9.19a1.67 1.67 0 0 1-2.36-2.36l8.49-8.48" />
      </svg>
    );
  }
  if (name === "sliders") {
    return (
      <svg {...props}>
        <path d="M4 21v-7" />
        <path d="M4 10V3" />
        <path d="M12 21v-9" />
        <path d="M12 8V3" />
        <path d="M20 21v-5" />
        <path d="M20 12V3" />
        <path d="M2 14h4" />
        <path d="M10 8h4" />
        <path d="M18 16h4" />
      </svg>
    );
  }
  if (name === "grid") {
    return (
      <svg {...props}>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
      </svg>
    );
  }
  if (name === "mic") {
    return (
      <svg {...props}>
        <rect x="9" y="3" width="6" height="11" rx="3" />
        <path d="M5 11a7 7 0 0 0 14 0" />
        <path d="M12 18v3" />
      </svg>
    );
  }
  if (name === "wave") {
    return (
      <svg {...props}>
        <path d="M4 12h2" />
        <path d="M8 8v8" />
        <path d="M12 5v14" />
        <path d="M16 8v8" />
        <path d="M20 12h0" />
      </svg>
    );
  }
  if (name === "speaker") {
    return (
      <svg {...props}>
        <path d="M5 10v4h4l5 4V6l-5 4z" />
        <path d="M18 9a4.5 4.5 0 0 1 0 6" />
      </svg>
    );
  }
  if (name === "speaker-stop") {
    return (
      <svg {...props}>
        <path d="M5 10v4h4l5 4V6l-5 4z" />
        <path d="M18 9l4 4" />
        <path d="M22 9l-4 4" />
      </svg>
    );
  }
  if (name === "trash") {
    return (
      <svg {...props}>
        <path d="M4 7h16" />
        <path d="M10 11v6" />
        <path d="M14 11v6" />
        <path d="M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
        <path d="M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />
      </svg>
    );
  }
  if (name === "send") {
    return (
      <svg {...props}>
        <path d="M22 2L11 13" />
        <path d="M22 2l-7 20-4-9-9-4z" />
      </svg>
    );
  }
  if (name === "image") {
    return (
      <svg {...props}>
        <rect x="3" y="4" width="18" height="16" rx="3" />
        <circle cx="9" cy="10" r="1.5" />
        <path d="M21 16l-5.5-5.5L7 19" />
      </svg>
    );
  }
  if (name === "thinking") {
    return (
      <svg {...props}>
        <path d="M9 18h6" />
        <path d="M10 22h4" />
        <path d="M8.5 14a6.5 6.5 0 1 1 7 0c-.85.67-1.5 1.77-1.5 3h-4c0-1.23-.65-2.33-1.5-3z" />
      </svg>
    );
  }
  if (name === "research") {
    return (
      <svg {...props}>
        <path d="M14 4.5a3 3 0 1 1 0 6 3 3 0 0 1 0-6z" />
        <path d="M10 18.5l8-8" />
        <path d="M7 12l-4 1 1 4 3-3" />
        <path d="M15 20l1-4 4-1-3 3" />
      </svg>
    );
  }
  if (name === "folder") {
    return (
      <svg {...props}>
        <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      </svg>
    );
  }
  if (name === "chevron-right") {
    return (
      <svg {...props}>
        <path d="M10 7l5 5-5 5" />
      </svg>
    );
  }
  if (name === "google") {
    return (
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
        <path
          d="M21.805 12.23c0-.72-.065-1.412-.184-2.077H12v3.93h5.498a4.702 4.702 0 0 1-2.038 3.088v2.565h3.302c1.932-1.78 3.043-4.404 3.043-7.506z"
          fill="#4285F4"
        />
        <path
          d="M12 22c2.76 0 5.077-.915 6.77-2.474l-3.302-2.565c-.915.614-2.086.977-3.468.977-2.669 0-4.93-1.803-5.735-4.226H2.852v2.646A10.222 10.222 0 0 0 12 22z"
          fill="#34A853"
        />
        <path
          d="M6.265 13.712A6.144 6.144 0 0 1 5.944 12c0-.594.107-1.17.321-1.712V7.642H2.852A10.222 10.222 0 0 0 1.778 12c0 1.648.394 3.208 1.074 4.358l3.413-2.646z"
          fill="#FBBC05"
        />
        <path
          d="M12 6.062c1.5 0 2.847.516 3.907 1.53l2.93-2.93C17.072 3.02 14.755 2 12 2 7.852 2 4.254 4.378 2.852 7.642l3.413 2.646c.805-2.423 3.066-4.226 5.735-4.226z"
          fill="#EA4335"
        />
      </svg>
    );
  }
  if (name === "github") {
    return (
      <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="currentColor"
          d="M12 .5a12 12 0 0 0-3.794 23.386c.6.11.82-.26.82-.577v-2.02c-3.338.726-4.042-1.61-4.042-1.61-.546-1.388-1.334-1.757-1.334-1.757-1.09-.744.082-.729.082-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.833 2.807 1.304 3.492.997.108-.775.418-1.305.762-1.605-2.665-.303-5.467-1.332-5.467-5.93 0-1.31.468-2.381 1.236-3.22-.124-.303-.536-1.524.117-3.176 0 0 1.008-.322 3.3 1.23A11.47 11.47 0 0 1 12 6.317c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.655 1.652.243 2.873.12 3.176.77.839 1.234 1.91 1.234 3.22 0 4.61-2.807 5.623-5.48 5.92.43.37.814 1.102.814 2.222v3.294c0 .32.216.693.825.576A12.002 12.002 0 0 0 12 .5Z"
        />
      </svg>
    );
  }
  return (
    <svg {...props}>
      <circle cx="12" cy="12" r="8" />
    </svg>
  );
}

export default function App() {
  const [authMode, setAuthMode] = useState("signin");
  const [authToken, setAuthToken] = useState(() => localStorage.getItem("kuberon-auth-token") || "");
  const [isAuthenticated, setIsAuthenticated] = useState(() => Boolean(localStorage.getItem("kuberon-auth-token")));
  const [profile, setProfile] = useState(() => ({
    name: localStorage.getItem("kuberon-user-name") || "Kuberon User",
    email: localStorage.getItem("kuberon-user-email") || "",
  }));
  const [loginForm, setLoginForm] = useState(() => ({
    name: localStorage.getItem("kuberon-user-name") || "",
    email: localStorage.getItem("kuberon-user-email") || "",
    password: "",
  }));
  const [sessionId, setSessionId] = useState(() => localStorage.getItem("kubeops-session") || createSessionId());
  const [theme, setTheme] = useState(() => localStorage.getItem("kuberon-theme") || "dark");
  const [namespace] = useState("default");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [isConnected, setIsConnected] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [isMobileViewport, setIsMobileViewport] = useState(() => window.innerWidth <= 920);
  const [searchQuery, setSearchQuery] = useState("");
  const [uiNotice, setUiNotice] = useState("");
  const [plusMenuOpen, setPlusMenuOpen] = useState(false);
  const [isVoiceListening, setIsVoiceListening] = useState(false);
  const [voiceModeEnabled, setVoiceModeEnabled] = useState(false);
  const [speakingMessageId, setSpeakingMessageId] = useState("");
  const socketRef = useRef(null);
  const messageLogRef = useRef(null);
  const textareaRef = useRef(null);
  const fileInputRef = useRef(null);
  const plusMenuRef = useRef(null);
  const pendingAssistantRef = useRef({ toolResults: [], fixes: [] });
  const recognitionRef = useRef(null);
  const speechRef = useRef(null);

  async function refreshSessions() {
    try {
      const response = await fetch("/api/sessions");
      const data = await response.json();
      setSessions(Array.isArray(data.sessions) ? data.sessions : []);
    } catch {
      setSessions([]);
    }
  }

  useEffect(() => {
    localStorage.setItem("kubeops-session", sessionId);
  }, [sessionId]);

  useEffect(() => {
    if (authToken) {
      localStorage.setItem("kuberon-auth-token", authToken);
    } else {
      localStorage.removeItem("kuberon-auth-token");
    }
  }, [authToken]);

  useEffect(() => {
    localStorage.setItem("kuberon-theme", theme);
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    if (!authToken) {
      return;
    }
    fetch("/api/auth/me", {
      headers: {
        Authorization: `Bearer ${authToken}`,
      },
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error("Session expired");
        }
        return response.json();
      })
      .then((data) => {
        setProfile(data.user);
        setIsAuthenticated(true);
      })
      .catch(() => {
        setAuthToken("");
        setIsAuthenticated(false);
      });
  }, [authToken]);

  useEffect(() => {
    const url = new URL(window.location.href);
    const tokenFromUrl = url.searchParams.get("auth_token");
    const authError = url.searchParams.get("auth_error");
    if (tokenFromUrl) {
      setAuthToken(tokenFromUrl);
      setIsAuthenticated(true);
      url.searchParams.delete("auth_token");
      window.history.replaceState({}, "", url.toString());
    }
    if (authError) {
      setUiNotice(`Google sign in failed: ${authError.replaceAll("_", " ")}`);
      url.searchParams.delete("auth_error");
      window.history.replaceState({}, "", url.toString());
    }
  }, []);

  useEffect(() => {
    return () => {
      if (recognitionRef.current) {
        recognitionRef.current.onresult = null;
        recognitionRef.current.onerror = null;
        recognitionRef.current.onend = null;
        recognitionRef.current.stop();
      }
    };
  }, []);

  useEffect(() => {
    function handleResize() {
      const nextIsMobile = window.innerWidth <= 920;
      setIsMobileViewport(nextIsMobile);
      if (!nextIsMobile) {
        setMobileSidebarOpen(false);
      }
    }

    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    return () => {
      if (window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [sessionId]);

  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${window.location.host}/ws/chat?session_id=${sessionId}`);
    socketRef.current = socket;

    socket.onopen = () => {
      setIsConnected(true);
      setConnectionMessage("");
    };
    socket.onerror = () => {
      setConnectionMessage("WebSocket connection failed. Check that the backend is still running on port 8000.");
    };
    socket.onclose = () => {
      setIsConnected(false);
      setConnectionMessage("Backend connection closed. Restart the API server and refresh if chat stops responding.");
    };
    socket.onmessage = (event) => {
      const packet = JSON.parse(event.data);
      startTransition(() => {
        if (packet.type === "session") {
          setConnectionMessage("");
        }
        if (packet.type === "tool_result") {
          pendingAssistantRef.current = {
            ...pendingAssistantRef.current,
            toolResults: [...pendingAssistantRef.current.toolResults, packet.payload],
          };
        }
        if (packet.type === "fixes") {
          pendingAssistantRef.current = {
            ...pendingAssistantRef.current,
            fixes: packet.payload || [],
          };
        }
        if (packet.type === "token") {
          setMessages((current) => {
            const next = [...current];
            const last = next[next.length - 1];
            if (!last || last.role !== "assistant" || last.streaming !== true) {
              next.push({
                role: "assistant",
                text: packet.payload,
                streaming: true,
                toolResults: pendingAssistantRef.current.toolResults,
                fixes: pendingAssistantRef.current.fixes,
                createdAt: new Date().toISOString(),
              });
            } else {
              last.text += packet.payload;
              last.toolResults = pendingAssistantRef.current.toolResults;
              last.fixes = pendingAssistantRef.current.fixes;
            }
            return [...next];
          });
        }
        if (packet.type === "final") {
          setMessages((current) =>
            current.map((item, index) =>
              index === current.length - 1 && item.role === "assistant"
                ? {
                    ...item,
                    text: packet.payload.message,
                    streaming: false,
                    toolResults: pendingAssistantRef.current.toolResults,
                    fixes: pendingAssistantRef.current.fixes,
                    createdAt: item.createdAt || new Date().toISOString(),
                  }
                : item,
            ),
          );
          pendingAssistantRef.current = { toolResults: [], fixes: [] };
          refreshSessions();
        }
      });
    };

    return () => socket.close();
  }, [sessionId]);

  useEffect(() => {
    fetch(`/api/sessions/${sessionId}/history`)
      .then((response) => response.json())
      .then((data) => {
        const history = [];
        for (const item of data.items || []) {
          history.push({ role: "user", text: item.user_message, createdAt: item.created_at });
          history.push({
            role: "assistant",
            text: item.assistant_message,
            streaming: false,
            toolResults: item.tool_calls || [],
            fixes: [],
            createdAt: item.created_at,
          });
        }
        setMessages(history);
      })
      .catch(() => setMessages([]));
  }, [sessionId]);

  useEffect(() => {
    if (messageLogRef.current) {
      messageLogRef.current.scrollTop = messageLogRef.current.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    if (!textareaRef.current) {
      return;
    }
    textareaRef.current.style.height = "0px";
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
  }, [input]);

  useEffect(() => {
    if (!uiNotice) {
      return;
    }
    const timer = window.setTimeout(() => setUiNotice(""), 2200);
    return () => window.clearTimeout(timer);
  }, [uiNotice]);

  useEffect(() => {
    function handlePointerDown(event) {
      if (!plusMenuRef.current?.contains(event.target)) {
        setPlusMenuOpen(false);
      }
    }

    if (plusMenuOpen) {
      document.addEventListener("mousedown", handlePointerDown);
    }

    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [plusMenuOpen]);

  const filteredHistory = useMemo(() => {
    if (!searchQuery.trim()) {
      return sessions;
    }
    const term = searchQuery.toLowerCase();
    return sessions.filter((item) =>
      [item.title, item.preview, item.session_id].some((value) => (value || "").toLowerCase().includes(term)),
    );
  }, [searchQuery, sessions]);

  function sendMessage(text) {
    const trimmed = text.trim();
    if (!trimmed) {
      return;
    }
    if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) {
      setConnectionMessage("Chat is not connected to the backend yet. Wait for the status to turn live, then try again.");
      return;
    }
    const createdAt = new Date().toISOString();
    if (messages.length === 0) {
      setSessions((current) => {
        const nextItem = {
          session_id: sessionId,
          title: deriveChatTitle(trimmed),
          preview: "Waiting for Kuberon to respond...",
          updated_at: createdAt,
        };
        return [nextItem, ...current.filter((item) => item.session_id !== sessionId)];
      });
    }
    setMessages((current) => [...current, { role: "user", text: trimmed, createdAt }]);
    pendingAssistantRef.current = { toolResults: [], fixes: [] };
    socketRef.current.send(JSON.stringify({ message: trimmed, namespace }));
    setInput("");
    setMobileSidebarOpen(false);
  }

  function handleComposerKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage(input);
    }
  }

  function resetConversation() {
    setMessages([]);
    setSessionId(createSessionId());
    setPlusMenuOpen(false);
    setMobileSidebarOpen(false);
  }

  function runPrompt(prompt) {
    sendMessage(prompt);
  }

  function openFiles() {
    fileInputRef.current?.click();
    setUiNotice("File picker opened.");
    setPlusMenuOpen(false);
  }

  function handleUiAction(label) {
    setUiNotice(`${label} is ready for a deeper backend integration later.`);
    setPlusMenuOpen(false);
  }

  function stopVoiceRecognition() {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
    }
    setIsVoiceListening(false);
  }

  function startVoiceRecognition({ autoSend = false } = {}) {
    const RecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!RecognitionCtor) {
      setUiNotice("Voice input is not supported in this browser. Try Chrome or Edge.");
      return;
    }

    if (isVoiceListening) {
      if (voiceModeEnabled !== autoSend) {
        stopVoiceRecognition();
      } else {
        stopVoiceRecognition();
        setVoiceModeEnabled(false);
        setUiNotice(autoSend ? "Voice mode stopped." : "Voice dictation stopped.");
        return;
      }
    }

    const recognition = new RecognitionCtor();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    recognition.continuous = false;
    recognitionRef.current = recognition;

    let finalTranscript = "";

    recognition.onresult = (event) => {
      let interimTranscript = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        const transcript = result[0]?.transcript || "";
        if (result.isFinal) {
          finalTranscript += transcript;
        } else {
          interimTranscript += transcript;
        }
      }

      const nextText = `${finalTranscript} ${interimTranscript}`.trim();
      setInput(nextText);
    };

    recognition.onerror = (event) => {
      setIsVoiceListening(false);
      setVoiceModeEnabled(false);
      if (event.error === "not-allowed") {
        setUiNotice("Microphone permission was denied. Allow microphone access and try again.");
        return;
      }
      if (event.error === "no-speech") {
        setUiNotice("No speech detected. Try again and speak a little closer to the mic.");
        return;
      }
      setUiNotice(`Voice input error: ${event.error}.`);
    };

    recognition.onend = () => {
      setIsVoiceListening(false);
      const spokenText = finalTranscript.trim() || textareaRef.current?.value.trim() || "";
      if (autoSend && spokenText) {
        sendMessage(spokenText);
      } else if (spokenText) {
        setInput(spokenText);
      }
      setVoiceModeEnabled(false);
    };

    setUiNotice(autoSend ? "Listening in voice mode..." : "Listening... Speak now.");
    setVoiceModeEnabled(autoSend);
    setIsVoiceListening(true);
    recognition.start();
  }

  function handleHistoryClick(item) {
    setMobileSidebarOpen(false);
    setMessages([]);
    setSessionId(item.session_id);
    setPlusMenuOpen(false);
  }

  function handleSidebarNav(actionId) {
    if (actionId === "new-chat") {
      resetConversation();
      return;
    }
    if (actionId === "search") {
      setUiNotice("Use the search box to filter your saved chats.");
      return;
    }
  }

  async function handleDeleteSession(event, item) {
    event.stopPropagation();
    try {
      const response = await fetch(`/api/sessions/${item.session_id}`, { method: "DELETE" });
      if (!response.ok) {
        throw new Error("Could not delete the chat.");
      }
      setSessions((current) => current.filter((entry) => entry.session_id !== item.session_id));
      if (item.session_id === sessionId) {
        setMessages([]);
        setSessionId(createSessionId());
      }
      setUiNotice("Chat deleted.");
    } catch (error) {
      setUiNotice(error.message || "Could not delete the chat.");
    }
  }

  function toggleSidebar() {
    if (isMobileViewport) {
      setMobileSidebarOpen((current) => !current);
      return;
    }
    setSidebarCollapsed((current) => !current);
  }

  function toggleTheme() {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }

  function handleLoginInputChange(event) {
    const { name, value } = event.target;
    setLoginForm((current) => ({ ...current, [name]: value }));
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    const endpoint = authMode === "signin" ? "/api/auth/signin" : "/api/auth/signup";
    const payload =
      authMode === "signin"
        ? { email: loginForm.email.trim(), password: loginForm.password }
        : { name: loginForm.name.trim(), email: loginForm.email.trim(), password: loginForm.password };

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || "Authentication failed.");
      }
      setProfile(data.user);
      localStorage.setItem("kuberon-user-name", data.user.name);
      localStorage.setItem("kuberon-user-email", data.user.email);
      setAuthToken(data.token);
      setIsAuthenticated(true);
      setUiNotice("");
      setLoginForm((current) => ({ ...current, password: "" }));
    } catch (error) {
      setUiNotice(error.message || "Authentication failed.");
    }
  }

  async function handleLogout() {
    if (authToken) {
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${authToken}`,
        },
      }).catch(() => undefined);
    }
    localStorage.removeItem("kuberon-user-name");
    localStorage.removeItem("kuberon-user-email");
    setProfile({ name: "Kuberon User", email: "" });
    setAuthToken("");
    setIsAuthenticated(false);
    setUiNotice("");
  }

  function handleAuthUtility(label) {
    setUiNotice(`${label} can be connected to real auth flows next.`);
  }

  function startGoogleSignIn() {
    const frontendRedirect = `${window.location.origin}${window.location.pathname}`;
    window.location.href = `${BACKEND_BASE_URL}/api/auth/google/start?frontend_redirect=${encodeURIComponent(frontendRedirect)}`;
  }

  function renderComposer(extraClass = "") {
    const showShortcuts = messages.length === 0;

    return (
      <div className={`composer-shell ${extraClass}`.trim()}>
        {connectionMessage || uiNotice ? <div className="composer-note">{connectionMessage || uiNotice}</div> : null}

        <div className="composer-box">
          <div className="composer-main">
            <div className="composer-plus-wrap" ref={plusMenuRef}>
              <button
                type="button"
                className={`composer-plus ${plusMenuOpen ? "open" : ""}`}
                aria-label="Open tools menu"
                aria-expanded={plusMenuOpen}
                onClick={() => setPlusMenuOpen((current) => !current)}
              >
                <Icon name="new-chat" />
              </button>
              {plusMenuOpen ? (
                <div className="plus-menu">
                  <button type="button" className="plus-menu-item" onClick={openFiles}>
                    <span className="plus-menu-icon"><Icon name="attach" /></span>
                    <span>Add photos &amp; files</span>
                  </button>
                  <button type="button" className="plus-menu-item" onClick={() => handleUiAction("Create image")}>
                    <span className="plus-menu-icon"><Icon name="image" /></span>
                    <span>Create image</span>
                  </button>
                  <button type="button" className="plus-menu-item" onClick={() => handleUiAction("Thinking")}>
                    <span className="plus-menu-icon"><Icon name="thinking" /></span>
                    <span>Thinking</span>
                  </button>
                  <button type="button" className="plus-menu-item" onClick={() => handleUiAction("Deep research")}>
                    <span className="plus-menu-icon"><Icon name="research" /></span>
                    <span>Deep research</span>
                  </button>
                  <button type="button" className="plus-menu-item has-arrow" onClick={() => handleUiAction("More tools")}>
                    <span className="plus-menu-icon"><Icon name="more" /></span>
                    <span>More</span>
                    <span className="plus-menu-arrow"><Icon name="chevron-right" /></span>
                  </button>
                </div>
              ) : null}
            </div>
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleComposerKeyDown}
              placeholder="Ask anything"
            />
            <div className="composer-right">
              <button
                type="button"
                className={`composer-icon ${isVoiceListening && !voiceModeEnabled ? "active" : ""}`}
                aria-label="Voice"
                onClick={() => startVoiceRecognition({ autoSend: false })}
              >
                <Icon name="mic" />
              </button>
              <button
                type="button"
                className={`composer-icon ${isVoiceListening && voiceModeEnabled ? "active" : ""}`}
                aria-label="Voice mode"
                onClick={() => startVoiceRecognition({ autoSend: true })}
              >
                <Icon name="wave" />
              </button>
              <button type="button" className="composer-send" disabled={!isConnected} aria-label="Send" onClick={() => sendMessage(input)}>
                <Icon name="send" />
              </button>
            </div>
          </div>

          <div className="composer-toolbar">
          </div>
        </div>

        {showShortcuts ? (
          <div className="shortcut-row">
            {starterPrompts.map((chip) => (
              <button key={chip} type="button" className="shortcut-chip" onClick={() => runPrompt(chip)}>
                {chip}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  function toggleSpeakMessage(messageId, text) {
    if (!window.speechSynthesis) {
      setUiNotice("Read aloud is not supported in this browser.");
      return;
    }

    if (speakingMessageId === messageId) {
      window.speechSynthesis.cancel();
      speechRef.current = null;
      setSpeakingMessageId("");
      return;
    }

    const content = stripForSpeech(text);
    if (!content) {
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(content);
    utterance.rate = 1;
    utterance.pitch = 1;
    utterance.onend = () => {
      speechRef.current = null;
      setSpeakingMessageId("");
    };
    utterance.onerror = () => {
      speechRef.current = null;
      setSpeakingMessageId("");
      setUiNotice("Could not read this message aloud.");
    };
    speechRef.current = utterance;
    setSpeakingMessageId(messageId);
    window.speechSynthesis.speak(utterance);
  }

  function renderAssistantMessage(message) {
    const messageId = `${message.createdAt || "assistant"}-${message.text.slice(0, 24)}`;
    const blocks = parseAssistantBlocks(message.text);
    const followUps = [];
    let insideFollowUps = false;

    for (const block of blocks) {
      if (block.type === "heading") {
        insideFollowUps = block.text === "Follow-ups";
        continue;
      }
      if (insideFollowUps && block.type === "list") {
        followUps.push(...block.items);
      }
    }

    const visibleBlocks = blocks.filter((block, index) => {
      if (block.type === "heading" && block.text === "Follow-ups") {
        return false;
      }
      const previous = blocks[index - 1];
      if (followUps.length && block.type === "list" && previous?.type === "heading" && previous.text === "Follow-ups") {
        return false;
      }
      return true;
    });

    return (
      <div className="assistant-response">
        <div className="assistant-response-body">
          {visibleBlocks.length ? renderAssistantBlocks(visibleBlocks, messageId) : <p className="assistant-paragraph">{message.text}</p>}
        </div>
        <div className="assistant-actions">
          <button
            type="button"
            className={`assistant-action-button ${speakingMessageId === messageId ? "active" : ""}`}
            onClick={() => toggleSpeakMessage(messageId, message.text)}
            aria-label={speakingMessageId === messageId ? "Stop reading aloud" : "Read aloud"}
            title={speakingMessageId === messageId ? "Stop reading aloud" : "Read aloud"}
          >
            <Icon name={speakingMessageId === messageId ? "speaker-stop" : "speaker"} />
            <span>{speakingMessageId === messageId ? "Stop" : "Listen"}</span>
          </button>
        </div>
        {followUps.length ? (
          <div className="assistant-followups">
            {followUps.map((item) => (
              <button key={item} type="button" className="assistant-followup-button" onClick={() => runPrompt(item)}>
                {item}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    );
  }

  function renderUserMessage(message) {
    return (
      <div className="user-bubble">
        <p>{message.text}</p>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="showcase-shell auth-shell">
        <div className="auth-card">
          <div className="auth-brand">
            <span className="auth-logo"><Icon name="logo" /></span>
            <div className="auth-brand-copy">
              <span className="brand-name">
                <span className="brand-name-light">kube</span>
                <span className="brand-name-accent">ron</span>
              </span>
              <p>AI workspace for Kubernetes troubleshooting</p>
            </div>
            <button
              className="icon-button theme-toggle auth-theme-toggle"
              type="button"
              aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              onClick={toggleTheme}
              title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            >
              <Icon name={theme === "dark" ? "theme-light" : "theme-dark"} />
            </button>
          </div>

          <div className="auth-content">
            <div className="auth-copy">
              <div className="hero-orb auth-hero"><Icon name="sparkle" /></div>
              <h1>Welcome to Kuberon</h1>
              <p>Sign in to continue into your Kubernetes incident workspace, diagnose faster, and collaborate with clarity.</p>
              <div className="auth-benefits">
                <div className="auth-benefit">
                  <span className="auth-benefit-check">✓</span>
                  <span>AI agent runs kubectl autonomously</span>
                </div>
                <div className="auth-benefit">
                  <span className="auth-benefit-check">✓</span>
                  <span>Root-cause analysis with fix recommendations</span>
                </div>
                <div className="auth-benefit">
                  <span className="auth-benefit-check">✓</span>
                  <span>Multi-turn conversation memory across namespaces</span>
                </div>
              </div>
            </div>

            <form className="auth-form" onSubmit={handleLoginSubmit}>
              <div className="auth-tabs">
                <button
                  type="button"
                  className={`auth-tab ${authMode === "signin" ? "active" : ""}`}
                  onClick={() => setAuthMode("signin")}
                >
                  Sign in
                </button>
                <button
                  type="button"
                  className={`auth-tab ${authMode === "signup" ? "active" : ""}`}
                  onClick={() => setAuthMode("signup")}
                >
                  Sign up
                </button>
              </div>
              <div className="auth-form-copy">
                <h2>{authMode === "signin" ? "Welcome back" : "Create your account"}</h2>
                <p>
                  {authMode === "signin"
                    ? "Enter your credentials to continue"
                    : "Set up your workspace access to get started"}
                </p>
              </div>
              {authMode === "signup" ? (
                <label className="auth-field">
                  <span>Name</span>
                  <input
                    type="text"
                    name="name"
                    value={loginForm.name}
                    onChange={handleLoginInputChange}
                    placeholder="Your full name"
                  />
                </label>
              ) : null}
              <label className="auth-field">
                <span>Email</span>
                <input
                  type="email"
                  name="email"
                  value={loginForm.email}
                  onChange={handleLoginInputChange}
                  placeholder="you@company.com"
                />
              </label>
              <label className="auth-field">
                <span>Password</span>
                <input
                  type="password"
                  name="password"
                  value={loginForm.password}
                  onChange={handleLoginInputChange}
                  placeholder="Enter your password"
                />
              </label>
              <button type="button" className="auth-link auth-forgot" onClick={() => handleAuthUtility("Forgot password")}>
                Forgot password?
              </button>
              <button className="auth-submit" type="submit">
                {authMode === "signin" ? "Continue to workspace" : "Create account"}
              </button>
              <div className="auth-divider">
                <span />
                <strong>or continue with</strong>
                <span />
              </div>
              <div className="auth-socials">
                <button type="button" className="auth-social" onClick={startGoogleSignIn}>
                  <span className="auth-social-mark google"><Icon name="google" /></span>
                  <span>Google</span>
                </button>
                <button type="button" className="auth-social" onClick={() => handleAuthUtility("GitHub sign in")}>
                  <span className="auth-social-mark github"><Icon name="github" /></span>
                  <span>GitHub</span>
                </button>
              </div>
              <p className="auth-switch">
                {authMode === "signin" ? "Don't have an account?" : "Already have an account?"}{" "}
                <button
                  type="button"
                  className="auth-link inline"
                  onClick={() => setAuthMode((current) => (current === "signin" ? "signup" : "signin"))}
                >
                  {authMode === "signin" ? "Sign up free" : "Sign in"}
                </button>
              </p>
              {uiNotice ? <p className="auth-feedback">{uiNotice}</p> : null}
              <p className="auth-note">Email/password and Google auth are supported. GitHub is still a placeholder.</p>
            </form>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="showcase-shell">
      <div className={`chat-shell ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${mobileSidebarOpen ? "mobile-sidebar-open" : ""}`}>
        {mobileSidebarOpen ? (
          <button type="button" className="sidebar-overlay" aria-label="Close sidebar" onClick={() => setMobileSidebarOpen(false)} />
        ) : null}
        <aside className="chat-sidebar">
          <div className="sidebar-header">
            <button className="icon-button brand-button" type="button" aria-label="Kuberon">
              <Icon name="logo" />
            </button>
            <button
              className="icon-button collapse-button"
              type="button"
              aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              onClick={toggleSidebar}
            >
              <Icon name={isMobileViewport ? "menu" : "panel"} />
            </button>
          </div>

          <div className="sidebar-primary-stack">
            {sidebarNavItems.map((item) => (
              <button
                key={item.id}
                className={`sidebar-primary ${item.id === "new-chat" ? "emphasis" : ""}`}
                type="button"
                onClick={() => handleSidebarNav(item.id)}
              >
                <span className="sidebar-icon"><Icon name={item.icon} /></span>
                {!sidebarCollapsed ? <span>{item.label}</span> : null}
              </button>
            ))}
          </div>

          {!sidebarCollapsed ? (
            <label className="sidebar-search" aria-label="Search chats">
              <span className="sidebar-icon"><Icon name="search" /></span>
              <input
                type="text"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                placeholder="Search chats"
              />
            </label>
          ) : null}

          {!sidebarCollapsed ? (
            <div className="history-wrap">
              <p className="sidebar-section-title">Recents</p>
              <div className="history-list">
                {filteredHistory.map((item) => (
                  <div key={item.session_id} className={`history-item ${item.session_id === sessionId ? "active" : ""}`}>
                    <button className="history-open" type="button" onClick={() => handleHistoryClick(item)}>
                      <span className="history-copy">
                        <strong>{formatSessionTitle(item.title)}</strong>
                      </span>
                    </button>
                    <button
                      type="button"
                      className="history-delete"
                      aria-label={`Delete ${formatSessionTitle(item.title)}`}
                      title="Delete chat"
                      onClick={(event) => handleDeleteSession(event, item)}
                    >
                      <Icon name="trash" />
                    </button>
                  </div>
                ))}
                {!filteredHistory.length ? (
                  <div className="history-empty">Start a new chat and it will appear here.</div>
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="sidebar-footer">
            <button className="account-button" type="button" onClick={handleLogout}>
              <span className="account-avatar"><Icon name="account" /></span>
              {!sidebarCollapsed ? (
                <span className="account-meta">
                  <strong>{profile.name}</strong>
                  <small>{profile.email || "john@example.com"}</small>
                </span>
              ) : null}
              {!sidebarCollapsed ? (
                <span className="account-settings">
                  <Icon name="sliders" />
                </span>
              ) : null}
            </button>
          </div>
        </aside>

        <main className={`chat-stage ${messages.length === 0 ? "home-mode" : ""}`}>
          <header className="chat-topbar">
            <div className="topbar-left">
              <button
                className="icon-button mobile-sidebar-toggle"
                type="button"
                aria-label="Toggle sidebar"
                onClick={toggleSidebar}
              >
                <Icon name="menu" />
              </button>
              <span className="chat-title-icon chat-title-logo">
                <Icon name="logo" />
              </span>
              <div className="chat-title-copy">
                <strong>Kuberon</strong>
              </div>
            </div>
            <div className="topbar-right">
              <button
                className="icon-button theme-toggle"
                type="button"
                aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
                onClick={toggleTheme}
                title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
              >
                <Icon name={theme === "dark" ? "theme-light" : "theme-dark"} />
              </button>
            </div>
          </header>

          <section className="chat-center" ref={messageLogRef}>
            {messages.length === 0 ? (
              <div className="home-stack">
                <div className="empty-state">
                  <div className="hero-orb"><Icon name="sparkle" /></div>
                  <h1>Ready when you are.</h1>
                </div>
                {renderComposer("home-mode")}
              </div>
            ) : (
              <div className="messages-stack">
                {messages.map((message, index) => (
                  <article className={`message-row ${message.role}`} key={`${message.role}-${index}`}>
                    <div className="message-content">
                      {message.role === "assistant" ? renderAssistantMessage(message) : renderUserMessage(message)}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>

          {messages.length > 0 ? <footer>{renderComposer()}</footer> : null}
        </main>
      </div>

      <input ref={fileInputRef} type="file" multiple hidden />
    </div>
  );
}
