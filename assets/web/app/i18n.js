"use strict";

let displayLanguagePreference = "system";
let displayLocale = browserLocale();

function browserLocale() {
  return /^zh(?:[-_]|$)/i.test(globalThis.navigator?.language || "")
    ? "zh-CN"
    : "en";
}

function interfaceLocale() {
  return displayLocale;
}

function interfaceLanguagePreference() {
  return displayLanguagePreference;
}

function t(chinese, english) {
  return displayLocale === "zh-CN" ? chinese : english;
}

function languagePreferenceOptions() {
  return [
    ["system", t("跟随系统", "Follow system")],
    ["zh-CN", "简体中文"],
    ["en", "English"],
  ];
}

function applyInterfaceLanguage(settings) {
  displayLanguagePreference = ["zh-CN", "en"].includes(settings?.preference)
    ? settings.preference
    : "system";
  const nativeLanguage = settings?.system_language;
  displayLocale =
    displayLanguagePreference !== "system"
      ? displayLanguagePreference
      : nativeLanguage
        ? /^zh(?:[-_]|$)/i.test(nativeLanguage)
          ? "zh-CN"
          : "en"
        : browserLocale();

  document.documentElement.lang = displayLocale;
  for (const element of document.querySelectorAll("[data-en]")) {
    if (element.dataset.zh === undefined)
      element.dataset.zh = element.textContent;
    element.textContent = t(element.dataset.zh, element.dataset.en);
  }
  for (const [attribute, key] of [
    ["aria-label", "enLabel"],
    ["title", "enTitle"],
  ]) {
    for (const element of document.querySelectorAll(
      attribute === "title" ? "[data-en-title]" : "[data-en-label]",
    )) {
      const chineseKey = key === "enTitle" ? "zhTitle" : "zhLabel";
      if (element.dataset[chineseKey] === undefined)
        element.dataset[chineseKey] = element.getAttribute(attribute) || "";
      element.setAttribute(
        attribute,
        t(element.dataset[chineseKey], element.dataset[key]),
      );
    }
  }
}

async function setInterfaceLanguage(preference) {
  applyInterfaceLanguage(await api("set-interface-language", { preference }));
}
