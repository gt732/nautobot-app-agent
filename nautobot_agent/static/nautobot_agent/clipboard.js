(() => {
    "use strict";

    async function copyText(text, { input = null } = {}) {
        try {
            if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
            await navigator.clipboard.writeText(text);
            return true;
        } catch {
            // Older browsers and insecure contexts may still support a user-initiated copy.
        }

        const focused = document.activeElement;
        const selection = focused === input ? [input.selectionStart, input.selectionEnd, input.selectionDirection] : null;
        let textarea = null;
        let copied = false;
        try {
            textarea = document.createElement("textarea");
            textarea.value = text;
            textarea.tabIndex = -1;
            textarea.style.cssText = "position: fixed; left: -9999px; top: 0;";
            document.body.append(textarea);
            textarea.select();
            copied = document.execCommand("copy");
        } catch {
            copied = false;
        } finally {
            textarea?.remove();
            focused?.focus({ preventScroll: true });
            if (selection) input.setSelectionRange(...selection);
        }
        return copied;
    }

    window.NautobotAgentClipboard = { copyText };
})();
