function localizeTimes() {
  for (const node of document.querySelectorAll("time")) {
    node.textContent = new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "medium",
    }).format(new Date(node.dateTime));
  }
}

async function refreshRun() {
  if (document.body.dataset.active !== "1") return;
  try {
    const response = await fetch(location.href, {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) throw new Error("unavailable");
    const page = new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
    const main = page.querySelector("main[data-reconciliation]");
    if (!main) throw new Error("unavailable");
    document.querySelector("main").replaceWith(main);
    document.body.dataset.active = page.body.dataset.active;
    localizeTimes();
  } catch {
    const status = document.querySelector('[role="status"]');
    if (status) status.textContent = "暂时无法更新进度，正在重试。";
  }
  if (document.body.dataset.active === "1") setTimeout(refreshRun, 6000);
}

localizeTimes();
if (document.body.dataset.active === "1") setTimeout(refreshRun, 6000);
