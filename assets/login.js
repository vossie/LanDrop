const accessCode = document.getElementById("accessCode");
const loginBtn = document.getElementById("loginBtn");
const loginStatus = document.getElementById("loginStatus");

async function login() {
  loginStatus.textContent = "Checking…";
  try {
    const response = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: accessCode.value })
    });
    if (!response.ok) {
      loginStatus.textContent = "Wrong access code.";
      return;
    }
    window.location.href = "/workspaces";
  } catch (error) {
    loginStatus.textContent = "Login failed.";
  }
}

loginBtn.addEventListener("click", login);
accessCode.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    login();
  }
});
