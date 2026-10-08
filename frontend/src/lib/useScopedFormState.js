import { useEffect, useRef, useState } from "react";

/**
 * useScopedFormState
 *
 * React hook for settings forms whose data comes from a parent that re-fetches
 * on many triggers (top-nav pipeline switches, post-save refreshes, sibling
 * component renders, etc.). The naive pattern:
 *
 *     const [form, setForm] = useState(initial);
 *     useEffect(() => setForm(initial), [initial]);
 *
 * is a known React antipattern: any prop re-render wipes unsaved local edits,
 * which manifests as "I edited Q5, then the form blanked back to the saved
 * value mid-typing" or "I clicked Save then Save again, and the second Save
 * overwrote my edits with stale data".
 *
 * THIS hook only resets local state when the SCOPE changes (`pipelineId`
 * shifted = user genuinely switched scope = it's correct to load fresh data).
 * On every other prop change (background refresh, sibling re-render, etc.)
 * the local form is preserved as the source of truth.
 *
 * Usage:
 *     const [form, setForm] = useScopedFormState(
 *         () => settings.recruiter_profile || {},  // selector — runs on scope change
 *         pipelineId,                                // scope key
 *         settings,                                  // dep that triggers re-eval
 *     );
 */
export function useScopedFormState(selector, scopeKey, deps) {
    const [form, setForm] = useState(selector);
    const lastLoadedScopeRef = useRef(scopeKey);
    useEffect(() => {
        if (lastLoadedScopeRef.current !== scopeKey) {
            lastLoadedScopeRef.current = scopeKey;
            setForm(selector());
        }
        // We deliberately depend on BOTH scopeKey and deps so the latest
        // selector (which closes over deps) runs on scope change.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [scopeKey, deps]);
    return [form, setForm];
}
