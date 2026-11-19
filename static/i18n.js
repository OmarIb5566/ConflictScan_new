/* ConflictScan - Arabic interface (right-to-left)
 * -----------------------------------------------------------------------
 * Chosen per person (account menu -> Language), remembered in their
 * preferences. Fixed labels - buttons, headings, steps, menus, statuses,
 * placeholders - are translated as they appear on screen; sentences built
 * from data (finding text, RFI text, file names, counts) stay as they are.
 * The layout flips to right-to-left.
 */
"use strict";
(() => {
const CS = window.CS;
const AR = {
  "Sign in": "تسجيل الدخول", "Create account": "إنشاء حساب", "Username": "اسم المستخدم", "Password": "كلمة المرور",
  "Display name": "الاسم المعروض", "Keep me signed in": "تذكرني", "Sign out": "تسجيل الخروج",
  "Home": "الرئيسية", "Admin": "الإدارة", "Trash": "سلة المحذوفات", "Help": "مساعدة", "+ New scan": "+ فحص جديد", "New scan": "فحص جديد",
  "Scans in progress": "عمليات فحص جارية", "Pinned": "مثبتة", "Recent projects": "مشروعات حديثة", "Nothing running.": "لا يوجد فحص جارٍ.",
  "Projects": "المشروعات", "Project": "المشروع", "Scans": "عمليات الفحص", "Documents": "المستندات", "Description": "الوصف",
  "Details": "التفاصيل", "Scan": "الفحص", "Findings": "الملاحظات", "RFIs": "طلبات الاستيضاح", "RFI log": "سجل طلبات الاستيضاح",
  "Members": "الأعضاء", "Settings": "الإعدادات", "Open latest results": "فتح آخر النتائج", "Edit & re-run": "تعديل وإعادة الفحص",
  "Download all (.zip)": "تنزيل الكل (.zip)", "Cancel scan": "إلغاء الفحص", "Run conflict scan": "تشغيل فحص التعارضات",
  "Next: Documents →": "التالي: المستندات ←", "Next: Scan →": "التالي: الفحص ←", "← Details": "→ التفاصيل", "← Documents": "→ المستندات",
  "Set A": "المجموعة أ", "Set B": "المجموعة ب", "Drop files here, or click to browse": "اسحب الملفات هنا أو انقر للاختيار",
  "…or paste text": "…أو الصق نصًا", "Scan settings": "إعدادات الفحص", "Ready to scan?": "جاهز للفحص؟", "Review focus": "محور المراجعة",
  "Contract order of precedence": "ترتيب أولوية المستندات التعاقدية", "RFI drafting": "صياغة طلبات الاستيضاح",
  "RFI form details": "بيانات نموذج طلب الاستيضاح", "Consultant": "الاستشاري", "Employer": "المالك", "Project code": "كود المشروع",
  "To": "إلى", "Location": "الموقع", "Level": "المنسوب", "Clear all": "مسح الكل",
  "Open": "مفتوحة", "Accept": "قبول", "Dismiss": "استبعاد", "Reopen": "إعادة فتح", "Accepted": "مقبولة", "Dismissed": "مستبعدة",
  "Any status": "أي حالة", "Any scope": "أي نطاق", "Any issue type": "أي نوع", "As found": "حسب الترتيب", "Expand all": "توسيع الكل",
  "Collapse all": "طي الكل", "Rebuild register": "إعادة إنشاء السجل", "Save view": "حفظ العرض", "Print": "طباعة",
  "Saved views…": "عروض محفوظة…", "Compare…": "مقارنة…", "Show resolved": "عرض ما تم حله", "Hide resolved": "إخفاء ما تم حله",
  "Select all shown": "تحديد الكل", "Clear": "مسح", "Assign to…": "إسناد إلى…", "Comment": "تعليق", "Comments": "التعليقات",
  "view in document": "عرض في المستند", "Impact:": "الأثر:", "Clarification sought:": "الاستيضاح المطلوب:",
  "+ Add": "+ إضافة", "Status": "الحالة", "Discipline": "التخصص", "Priority": "الأولوية", "Response required by": "مطلوب الرد قبل",
  "Subject": "الموضوع", "Background": "الخلفية", "Proposed solution": "الحل المقترح", "Attachments": "المرفقات",
  "Add to RFI log": "إضافة إلى سجل طلبات الاستيضاح", "Delete": "حذف", "Revert to AI draft": "الرجوع إلى مسودة الذكاء الاصطناعي",
  "Draft": "مسودة", "In review": "قيد المراجعة", "Approved": "معتمد", "Issued": "صادر", "Response received": "تم استلام الرد",
  "Closed": "مغلق", "Void": "ملغى", "Overdue": "متأخر", "Export to Excel": "تصدير إلى إكسل", "Import existing log": "استيراد سجل قائم",
  "+ New RFI": "+ طلب استيضاح جديد", "Submit for review": "إرسال للمراجعة", "Approve": "اعتماد", "Mark as issued": "تحديد كصادر",
  "Close": "إغلاق", "Save": "حفظ", "Cancel": "إلغاء", "Done": "تم", "Got it": "فهمت", "Next": "التالي", "Skip": "تخطي",
  "Restore": "استعادة", "Notifications": "الإشعارات", "Mark all read": "تحديد الكل كمقروء", "Account settings": "إعدادات الحساب",
  "Keyboard shortcuts": "اختصارات لوحة المفاتيح", "Complete": "مكتمل", "Failed": "فشل", "Running": "قيد التشغيل", "Queued": "في الانتظار",
  "Cancelled": "ملغى", "critical": "حرجة", "major": "رئيسية", "minor": "ثانوية", "Users": "المستخدمون", "Scan queue": "طابور الفحص",
  "AI fleet": "أسطول الذكاء الاصطناعي", "System health": "حالة النظام", "Usage": "الاستخدام", "Audit log": "سجل التدقيق",
  "Admin console": "لوحة الإدارة", "+ Add user": "+ إضافة مستخدم", "Role": "الدور", "Name": "الاسم", "Activity": "النشاط",
  "Take the tour": "جولة تعريفية", "What's new": "ما الجديد", "View only": "عرض فقط", "Rev": "مراجعة",
  "Search projects, scans, actions…": "ابحث في المشروعات والفحوص والإجراءات…", "Search findings (/)": "ابحث في الملاحظات (/)",
  "Search RFIs (/)": "ابحث في طلبات الاستيضاح (/)", "Reviewer note (saved automatically)": "ملاحظة المراجع (تحفظ تلقائيًا)",
  "Add a comment - type @ and a username to notify someone": "أضف تعليقًا - اكتب @ واسم المستخدم لتنبيه زميل",
};
const ATTRS = ["placeholder", "title", "aria-label"];
let lang = "en";

function translateNode(root) {
  if (lang !== "ar" || !root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const t = n.nodeValue.trim();
    if (!t || n.parentElement.closest("textarea, .says, .c-body, pre, .mono, [contenteditable]")) continue;
    if (AR[t]) { n.nodeValue = n.nodeValue.replace(t, AR[t]); continue; }
    // labels with an icon or punctuation around them, e.g. "⌂ Home"
    const m = t.match(/^([^\p{L}]*)(\p{L}[\s\S]*?\p{L}|\p{L})([^\p{L}]*)$/u);
    if (m && AR[m[2]]) n.nodeValue = n.nodeValue.replace(m[2], AR[m[2]]);
  }
  if (root.querySelectorAll) {
    for (const elx of [root, ...root.querySelectorAll("[placeholder], [title], [aria-label]")]) {
      for (const a of ATTRS) {
        const v = elx.getAttribute && elx.getAttribute(a);
        if (v && AR[v.trim()]) elx.setAttribute(a, AR[v.trim()]);
      }
    }
  }
}

function apply(next) {
  lang = next === "ar" ? "ar" : "en";
  document.documentElement.lang = lang;
  document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
  if (lang === "ar") translateNode(document.body);
}

new MutationObserver((muts) => {
  if (lang !== "ar") return;
  for (const m of muts) for (const n of m.addedNodes) {
    if (n.nodeType === 1) translateNode(n);
    else if (n.nodeType === 3 && n.parentElement) translateNode(n.parentElement);
  }
}).observe(document.documentElement, { childList: true, subtree: true });

try { apply(localStorage.getItem("cs.lang") || "en"); } catch { /* storage blocked */ }

CS.setLanguage = (next) => {
  try { localStorage.setItem("cs.lang", next); } catch { /* ignore */ }
  CS.savePrefs({ ui: { ...(CS.App.prefs.ui || {}), lang: next } });
  location.reload();
};
CS.BOOT_HOOKS.push(() => {
  const saved = (CS.App.prefs.ui || {}).lang;
  if (saved && saved !== lang) { try { localStorage.setItem("cs.lang", saved); } catch { /* ignore */ } apply(saved); }
});
CS.PALETTE_ITEMS.push(() => [{ label: lang === "ar" ? "English interface" : "الواجهة العربية (Arabic interface)",
  run: () => CS.setLanguage(lang === "ar" ? "en" : "ar") }]);
CS.language = () => lang;
})();
