import { useEffect, useState, useRef } from "react";
import {
    AlertDialog,
    AlertDialogContent,
    AlertDialogHeader,
    AlertDialogTitle,
    AlertDialogDescription,
    AlertDialogFooter,
} from "@/components/ui/alert-dialog";

/**
 * Promise-based replacements for window.confirm / window.prompt, styled like
 * the rest of the app. Mount <DialogHost /> once (AppLayout) and call:
 *
 *   if (!(await confirmDialog({ title, description, confirmLabel, destructive }))) return;
 *   const name = await promptDialog({ title, placeholder });  // null = cancelled
 */
let _open = null; // set by DialogHost on mount

export function confirmDialog(opts) {
    if (!_open) return Promise.resolve(window.confirm(opts.description || opts.title));
    return _open({ kind: "confirm", ...opts });
}

export function promptDialog(opts) {
    if (!_open) return Promise.resolve(window.prompt(opts.title, opts.defaultValue || ""));
    return _open({ kind: "prompt", ...opts });
}

export function DialogHost() {
    const [req, setReq] = useState(null); // {kind,title,description,confirmLabel,destructive,placeholder,defaultValue,inputType,resolve}
    const [value, setValue] = useState("");
    const inputRef = useRef(null);

    useEffect(() => {
        _open = (opts) =>
            new Promise((resolve) => {
                setValue(opts.defaultValue || "");
                setReq({ ...opts, resolve });
            });
        return () => { _open = null; };
    }, []);

    useEffect(() => {
        if (req?.kind === "prompt") setTimeout(() => inputRef.current?.focus(), 50);
    }, [req]);

    const settle = (result) => {
        req?.resolve(result);
        setReq(null);
    };

    if (!req) return null;
    const isPrompt = req.kind === "prompt";

    return (
        <AlertDialog open onOpenChange={(o) => !o && settle(isPrompt ? null : false)}>
            <AlertDialogContent className="bg-[#141519] border-strokes max-w-md">
                <AlertDialogHeader>
                    <AlertDialogTitle className="font-heading text-base">{req.title}</AlertDialogTitle>
                    {req.description && (
                        <AlertDialogDescription className="text-sm text-ink-muted">
                            {req.description}
                        </AlertDialogDescription>
                    )}
                </AlertDialogHeader>
                {isPrompt && (
                    <input
                        ref={inputRef}
                        type={req.inputType || "text"}
                        className="input-dark"
                        placeholder={req.placeholder || ""}
                        value={value}
                        onChange={(e) => setValue(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && value.trim() && settle(value.trim())}
                        data-testid="prompt-dialog-input"
                    />
                )}
                <AlertDialogFooter>
                    <button
                        onClick={() => settle(isPrompt ? null : false)}
                        data-testid="dialog-cancel-btn"
                        className="btn-secondary !py-2 !px-4 text-sm"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={() => settle(isPrompt ? (value.trim() || null) : true)}
                        disabled={isPrompt && !value.trim()}
                        data-testid="dialog-confirm-btn"
                        className={`!py-2 !px-4 text-sm rounded-md font-medium transition-colors disabled:opacity-40 ${
                            req.destructive
                                ? "bg-red-500/15 border border-red-500/40 text-red-400 hover:bg-red-500/25"
                                : "btn-primary"
                        }`}
                    >
                        {req.confirmLabel || (isPrompt ? "Save" : "Confirm")}
                    </button>
                </AlertDialogFooter>
            </AlertDialogContent>
        </AlertDialog>
    );
}
