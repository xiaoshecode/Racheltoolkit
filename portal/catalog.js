const list = document.querySelector("#courses");
const status = document.querySelector("#status");
const retry = document.querySelector("#retry");
const direct = document.querySelector("#direct");

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
    const response = await fetch(new URL("/api/public/courses/", server), {signal, credentials: "omit"});
    if (!response.ok) throw new Error("Catalog unavailable");
    const {courses} = await response.json();
    const fragment = document.createDocumentFragment();
    for (const course of courses) {
      const entry = document.createElement("article");
      const title = document.createElement("h2");
      title.append(teachingLink(origin, course.url, course.title));
      const intro = document.createElement("p");
      intro.textContent = course.intro;
      const chapters = document.createElement("ol");
      for (const chapter of course.chapters) {
        const item = document.createElement("li");
        item.append(teachingLink(origin, chapter.url, chapter.title));
        chapters.append(item);
      }
      entry.append(title, intro, chapters);
      fragment.append(entry);
    }
    list.replaceChildren(fragment);
    status.textContent = courses.length ? "已发布 " + courses.length + " 门课程。" : "目前没有已发布课程。";
  } catch {
    status.textContent = "暂时无法读取课程。请重试，或直接打开教学站。";
  } finally {
    retry.disabled = false;
  }
}

retry.addEventListener("click", loadCatalog);
loadCatalog();
