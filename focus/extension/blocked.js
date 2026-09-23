// blocked.html 逻辑: 填充拦截原因 (MV3 CSP 禁止内联脚本, 必须独立文件)
const q = new URLSearchParams(location.search);

document.getElementById("domain").textContent = q.get("domain") || "未知站点";

const reason = q.get("reason");
const budget = parseInt(q.get("budget") || "0", 10);
const used = parseInt(q.get("used") || "0", 10);
const el = document.getElementById("reason");
if (reason === "always") {
    el.textContent = "该站点在工作时段被硬拦截（额度配置为 0）。";
} else if (reason === "budget") {
    el.innerHTML =
        `今日额度 <span class="budget">${budget} 分钟</span> 已用完（本站累计 ${used} 分钟）。` +
        `<br>剩下的工作时间，留给本来要做的事。`;
} else {
    el.textContent = "工作时段拦截生效中。";
}

document.getElementById("close").addEventListener("click", () => window.close());
