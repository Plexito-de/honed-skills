// Purpose: close every tab that shows one of the given URLs, in every running copy of one browser.
// Usage:   osascript -l JavaScript close-tabs.js <bundle id> <url> [<url> ...]    (prints the count)
//
// bin/review runs this after Send, for a review tab that the browser refused to let the page close.
//
// The Apple events go to each running copy by process id. A script that names the app reaches ONE
// copy, and with a second copy running (a headless Edge, a Chrome that Playwright drives) macOS
// delivered it to that copy in both cases measured on 2026-09-30, so the tab stayed open. The codes
// come from the browsers' scripting.sdef and are the same in Chrome and Edge: cwin is a window, CrTb a
// tab, "URL " and "ID  " are properties. Windows and tabs are named by id, never by index, because a
// close renumbers the rest, and a match is read again by its own id right before it is closed.
// 0x20000 is kAEDoNotPromptForUserConsent: a missing permission fails instead of asking.
ObjC.import("AppKit");
const D = $.NSAppleEventDescriptor;
const code = s => [...s].reduce((n, c) => n * 256 + c.charCodeAt(0), 0);
const type = s => D.descriptorWithTypeCode(code(s));
const ALL = D.descriptorWithDescriptorTypeData(code("abso"), type("all ").data);

function spec(want, from, form, seld) {
  const r = D.recordDescriptor;
  r.setDescriptorForKeyword(type(want), code("want"));
  r.setDescriptorForKeyword(from, code("from"));
  r.setDescriptorForKeyword(D.descriptorWithEnumCode(code(form)), code("form"));
  r.setDescriptorForKeyword(seld, code("seld"));
  return r.coerceToDescriptorType(code("obj "));
}

const property = (of, name) => spec("prop", of, "prop", type(name));

function send(pid, verb, target) {
  const event = D.appleEventWithEventClassEventIDTargetDescriptorReturnIDTransactionID(
    code("core"), code(verb), D.descriptorWithProcessIdentifier(pid), -1, 0);
  event.setParamDescriptorForKeyword(target, code("----"));
  const reply = event.sendEventWithOptionsTimeoutError(0x3 | 0x20000, 2, null);
  const result = reply.isNil() ? reply : reply.paramDescriptorForKeyword(code("----"));
  if (result.isNil()) return [];
  if (result.descriptorType !== code("list")) return [result];     // one value, not a list of them
  const items = [];
  for (let i = 1; i <= result.numberOfItems; i++) items.push(result.descriptorAtIndex(i));
  return items;
}

function run([bundleId, ...urls]) {
  const copies = $.NSRunningApplication.runningApplicationsWithBundleIdentifier(bundleId);
  const ours = d => urls.includes(d.stringValue.js);
  let closed = 0;
  for (let c = 0; c < copies.count; c++) {
    const pid = copies.objectAtIndex(c).processIdentifier;
    const everyWindow = spec("cwin", D.nullDescriptor, "indx", ALL);
    for (const windowId of send(pid, "getd", property(everyWindow, "ID  "))) {
      const window = spec("cwin", D.nullDescriptor, "ID  ", windowId);
      const tabs = spec("CrTb", window, "indx", ALL);
      const at = send(pid, "getd", property(tabs, "URL "));
      send(pid, "getd", property(tabs, "ID  ")).forEach((id, k) => {
        const tab = spec("CrTb", window, "ID  ", id);
        if (at[k] && ours(at[k]) && send(pid, "getd", property(tab, "URL ")).some(ours)) {
          send(pid, "clos", tab);
          closed++;
        }
      });
    }
  }
  return closed;
}
