import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

// Tailwind CSS — JIT 가 사용된 utility 만 인라인 주입
import "./styles/tailwind.css";

const container = document.getElementById("root");
if (container) {
  const root = createRoot(container);
  root.render(<App />);
}
