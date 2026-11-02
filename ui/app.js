"use strict";

const form = document.getElementById("ask-form");
const input = document.getElementById("question");
const submit = document.getElementById("submit");
const result = document.getElementById("result");
const answer = document.getElementById("answer");
const sqlDetails = document.getElementById("sql-details");
const sql = document.getElementById("sql");

// Everything is set through textContent: answers can quote statement text, which must never become markup.
function show(text, queries, isError) {
  answer.textContent = text;
  answer.classList.toggle("error", isError);
  sql.textContent = queries.join("\n\n");
  sqlDetails.hidden = queries.length === 0;
  result.hidden = false;
}

async function ask(question) {
  submit.disabled = true;
  submit.textContent = "Working it out…";
  try {
    const response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) {
      show(body.answer, body.sql, false);
    } else {
      const detail = typeof body.detail === "string" ? body.detail : "That question couldn't be processed.";
      show(detail, [], true);
    }
  } catch {
    show("Couldn't reach the service. Check your connection and try again.", [], true);
  } finally {
    submit.disabled = false;
    submit.textContent = "Ask";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (question) ask(question);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".example").forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.textContent;
    form.requestSubmit();
  });
});
