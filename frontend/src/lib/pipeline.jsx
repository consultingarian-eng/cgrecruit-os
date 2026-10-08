import { createContext, useContext, useEffect, useState } from "react";
import api from "./api";
import { DEFAULT_TZ } from "./formatET";

const PipelineContext = createContext(null);

export function PipelineProvider({ children }) {
    const [pipelines, setPipelines] = useState([]);
    const [activePipelineId, setActivePipelineId] = useState(() => localStorage.getItem("cgrecruit_pipeline") || null);
    const [loading, setLoading] = useState(true);
    const [timezone, setTimezone] = useState(DEFAULT_TZ);

    const refresh = async () => {
        try {
            const r = await api.get("/pipelines");
            setPipelines(r.data);
            if (r.data.length && !r.data.find((p) => p.id === activePipelineId)) {
                setActivePipelineId(r.data[0].id);
                localStorage.setItem("cgrecruit_pipeline", r.data[0].id);
            }
        } finally { setLoading(false); }
    };

    // Fetch the active pipeline's settings to get the configured timezone.
    // Runs whenever the active pipeline changes so switching pipelines
    // immediately re-locks all timestamp displays to the correct timezone.
    useEffect(() => {
        let cancelled = false;
        async function fetchTz() {
            try {
                const params = activePipelineId ? { pipeline_id: activePipelineId } : {};
                const r = await api.get("/settings", { params });
                const tz = r.data?.region_language?.timezone;
                if (!cancelled && tz) setTimezone(tz);
            } catch { /* keep current timezone on error */ }
        }
        fetchTz();
        return () => { cancelled = true; };
    }, [activePipelineId]);

    useEffect(() => { refresh(); /* eslint-disable-next-line */ }, []);

    const setActive = (id) => {
        setActivePipelineId(id);
        localStorage.setItem("cgrecruit_pipeline", id);
    };

    return (
        <PipelineContext.Provider value={{ pipelines, activePipelineId, setActive, refresh, loading, timezone }}>
            {children}
        </PipelineContext.Provider>
    );
}

export const usePipeline = () => useContext(PipelineContext);
