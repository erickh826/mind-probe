import { installReviewTabGuards } from "@mind-probe/shared";
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

installReviewTabGuards();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
