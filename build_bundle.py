"""将前端 ES modules 打包为单个 IIFE bundle。"""
import re
from pathlib import Path

ROOT = Path(__file__).parent / "frontend" / "js"

def read_file(rel_path):
    return (ROOT / rel_path).read_text(encoding='utf-8')

def strip_imports(code):
    """Remove all import lines."""
    return re.sub(r'^import\s+.*?;?\s*$', '', code, flags=re.MULTILINE)

def process_api(code):
    """api.js: remove exports, functions become top-level."""
    code = strip_imports(code)
    code = re.sub(r'^export\s+', '', code, flags=re.MULTILINE)
    return code

def process_util(code):
    """utils: remove imports/exports."""
    code = strip_imports(code)
    code = re.sub(r'^export\s+', '', code, flags=re.MULTILINE)
    return code

def process_store(code, var_name):
    """stores: convert 'export default function XXX()' to 'function XXX()'."""
    code = strip_imports(code)
    # "export default function mailStore() {" -> "function mailStore() {"
    code = re.sub(r'^export\s+default\s+function\s+', 'function ', code, flags=re.MULTILINE)
    return code

def process_component(code, comp_name):
    """
    Components use: export default () => ({ ... });
    Convert to: function compNameComp() { return ({ ... }); }
    """
    code = strip_imports(code)
    
    # Handle: export default () => ({
    # Replace with function wrapper, and fix the closing
    if 'export default () => ({' in code:
        code = code.replace('export default () => ({', f'function {comp_name}Comp() {{ return ({{', 1)
        # The file ends with "});" — we need to add the function closing brace
        # Find the last ");" and add "}" after it
        last_close = code.rfind('});')
        if last_close >= 0:
            code = code[:last_close] + '}); }' + code[last_close+3:]
    elif 'export default' in code:
        code = re.sub(r'export\s+default\s+', f'var {comp_name}Comp = ', code)
    
    return code


# Build the bundle
parts = []
parts.append("""/**
 * Quillbox - Bundled JS (auto-generated)
 * All modules combined into single IIFE to avoid ES module loading issues.
 */
(function() {
'use strict';
""")

# API
parts.append("// ========== api.js ==========")
parts.append(process_api(read_file("api.js")))
parts.append("""
// API namespace for stores
var api = {
  getEmails, getEmail, patchEmail, deleteEmail, sendEmail, forwardEmail,
  getActionStatus,
  getAccounts, getStats, triggerSync,
  getTodos, createTodo, patchTodo, deleteTodo, extractTodosBatch, extractTodosBatchStatus,
  getSettings, saveSettings, testAI, searchPeople, getContacts, getSenders,
  uploadAttachment,
  generateRule, evaluateRule,
  bulkEmails, getSavedSearches, addSavedSearch, delSavedSearch,
  getCategories, extractTodos,
  getDrafts, createDraft, patchDraft, deleteDraft,
  importOutlookRules, getMailboxLabels, saveMailboxLabels,
  getDashboard, getTopics, analyzeTopic, getTopicRules, addTopicRule, delTopicRule,
  getInboxTabs, addInboxTab, delInboxTab, previewTopic, topicFromNl, reorderTopicRules, updateTopicRule, getTopicEmails, getTopicCounts,
  addUserFolder, delUserFolder, addRule, delRule, applyRules, getUserFolders,
};
""")

# Utils
for util in ["utils/sse.js", "utils/sanitize.js", "utils/shortcuts.js"]:
    parts.append(f"// ========== {util} ==========")
    code = process_util(read_file(util))
    # Remove duplicate BASE_URL declaration (already in api.js)
    if 'BASE_URL' in code and util != "api.js":
        code = re.sub(r"^const BASE_URL\s*=.*$", "// (BASE_URL already declared)", code, flags=re.MULTILINE)
    parts.append(code)

# Stores
for store in ["stores/ui.js", "stores/mail.js", "stores/ai.js", "stores/settings.js"]:
    parts.append(f"// ========== {store} ==========")
    parts.append(process_store(read_file(store), Path(store).stem))

# Components
components = [
    ("components/emailList.js", "emailList"),
    ("components/emailDetail.js", "emailDetail"),
    ("components/sidebar.js", "sidebar"),
    ("components/aiChat.js", "aiChat"),
    ("components/compose.js", "compose"),
    ("components/settingsPanel.js", "settingsPanel"),
    ("components/commandPalette.js", "commandPalette"),
    ("components/searchBar.js", "searchBar"),
    ("components/topBar.js", "topBar"),
]
for filepath, name in components:
    parts.append(f"// ========== {filepath} ==========")
    parts.append(process_component(read_file(filepath), name))

# Registration
parts.append("""
// ========== Alpine Registration ==========
function _registerAll() {
  if (window.__quillboxModuleLoaded) return true;  // 防止重复注册导致 store 实例被替换、响应式失联
  if (typeof Alpine === 'undefined') return false;
  
  try {
    Alpine.store('ui', uiStore());
    Alpine.store('mail', mailStore());
    Alpine.store('ai', aiStore());
    Alpine.store('settings', settingsStore());

    Alpine.data('emailList', emailListComp);
    Alpine.data('emailDetail', emailDetailComp);
    Alpine.data('sidebar', sidebarComp);
    Alpine.data('aiChat', aiChatComp);
    Alpine.data('compose', composeComp);
    Alpine.data('settingsPanel', settingsPanelComp);
    Alpine.data('commandPalette', commandPaletteComp);
    Alpine.data('searchBar', searchBarComp);
    Alpine.data('topBar', topBarComp);

    window.__quillboxModuleLoaded = true;
    console.log('[Quillbox] Bundle: stores and components registered.');
    return true;
  } catch(e) {
    console.error('[Quillbox] Registration error:', e);
    return false;
  }
}

// Method 1: alpine:init event
document.addEventListener('alpine:init', function() {
  _registerAll();
});

// Method 2: poll for Alpine (in case event already fired)
(function() {
  if (_registerAll()) return;
  var attempts = 0;
  var timer = setInterval(function() {
    attempts++;
    if (_registerAll()) {
      clearInterval(timer);
    } else if (attempts > 600) {
      clearInterval(timer);
      console.error('[Quillbox] Alpine.js not found after 3s');
    }
  }, 5);
})();

// Keyboard shortcuts
document.addEventListener('DOMContentLoaded', function() {
  setTimeout(function() {
    if (typeof Alpine !== 'undefined') {
      var getStore = function(name) { return Alpine.store(name); };
      // 新界面 /app2 自带键盘处理（E=归档等，与旧版 E=删除冲突），设置 __QUILLBOX_V2 时不注册旧快捷键
      if (!window.__QUILLBOX_V2) {
        registerDefaultShortcuts(getStore);
        initShortcuts();
      }
    }
  }, 300);
});

})(); // end IIFE
""")

bundle = '\n'.join(parts)
out_path = ROOT / "bundle.js"
out_path.write_text(bundle, encoding='utf-8')
print(f"Bundle written: {out_path} ({len(bundle)} bytes, {bundle.count(chr(10))} lines)")

# Quick syntax validation
# 用当前解释器而不是写死 'python'：有的系统（如 macOS）上没有 python 命令（只有 python3），写死会在 bundle 写完后抛 FileNotFoundError
import subprocess
import sys
result = subprocess.run([sys.executable, '-c', f'import sys; code=open(r"{out_path}",encoding="utf-8").read(); print("Lines:",code.count(chr(10)))'],
                      capture_output=True, text=True)
print(result.stdout.strip())
