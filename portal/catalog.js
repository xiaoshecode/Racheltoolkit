const list = document.querySelector("#courses");
const status = document.querySelector("#status");
const retry = document.querySelector("#retry");
const direct = document.querySelector("#direct");
const search = document.createElement("form");
search.setAttribute("role", "search");
search.innerHTML = '<label>查找资料 <input name="q" type="search" maxlength="160" placeholder="章名、关键词或文件名"></label> <label>文件格式 <select name="format"><option value="">全部格式</option></select></label> <button>查找</button>';
for (const format of ["PPTX", "PPT", "PDF", "DOCX", "DOC"]) {
  search.elements.format.add(new Option(format, format));
}
status.before(search);
let generation = 0;

function teachingLink(origin, href, title) {
  const target = new URL(href, origin);
  if (target.origin !== origin || !target.pathname.startsWith("/courses/")) {
    throw new Error("Invalid course address");
  }
  const link = document.createElement("a");
  link.href = target.href;
  link.textContent = title;
  return link;
}

async function loadCatalog() {
  const current = ++generation;
  const filters = new URLSearchParams(new FormData(search));
  retry.disabled = true;
  list.replaceChildren();
  status.textContent = "正在读取课程目录…";
  const signal = AbortSignal.timeout(10000);
  try {
    const settings = await fetch(new URL("./config.json", import.meta.url), {signal, cache: "no-store"});
    if (!settings.ok) throw new Error("Configuration unavailable");
    const {origin} = await settings.json();
    if (!origin) {
      direct.hidden = true;
      status.textContent = "教学站地址尚未配置。";
      return;
    }
    const server = new URL(origin);
    if (server.origin !== origin || server.protocol !== "https:") throw new Error("Invalid teaching origin");
    direct.href = server.href;
    direct.hidden = false;
    const catalogUrl = new URL("/api/public/courses/", server);
    catalogUrl.search = filters.toString();
    const response = await fetch(catalogUrl, {signal, credentials: "omit"});
    if (!response.ok) throw new Error("Catalog unavailable");
    const {courses} = await response.json();
    const fragment = document.createDocumentFragment();
    for (const course of courses) {
      const entry = document.createElement("article");
      const title = document.createElement("h2");
      const courseLink = teachingLink(origin, course.url, course.title);
      courseLink.search = filters.toString();
      title.append(courseLink);
      const intro = document.createElement("p");
      intro.textContent = course.intro;
      const chapters = document.createElement("ol");
      for (const chapter of course.chapters) {
        const item = document.createElement("li");
        const link = teachingLink(origin, chapter.url, chapter.title);
        link.search = filters.toString();
        item.append(link, " · " + chapter.materials.length + " 份资料");
        chapters.append(item);
      }
      const stamp = document.createElement("small");
      stamp.textContent = "发布于 " + new Date(course.published_at).toLocaleDateString("zh-CN", {timeZone:"Asia/Shanghai"});
      entry.append(title, intro, stamp, chapters);
      fragment.append(entry);
    }
    if (current !== generation) return;
    list.replaceChildren(fragment);
    status.textContent = courses.length ? "找到 " + courses.length + " 门已发布课程。" :
      (filters.get("q") || filters.get("format") ? "没有符合条件的资料，请调整查找条件。" : "目前没有已发布课程。");
  } catch {
    if (current === generation) status.textContent = "暂时无法读取课程。请重试，或直接打开教学站。";
  } finally {
    if (current === generation) retry.disabled = false;
  }
}

retry.addEventListener("click", loadCatalog);
search.addEventListener("submit", event => {event.preventDefault();loadCatalog();});
loadCatalog();
