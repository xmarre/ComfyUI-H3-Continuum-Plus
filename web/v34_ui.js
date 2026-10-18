import { app } from "../../scripts/app.js";
import { installProjectWidgets, orderSampler, refreshProjectWidgets } from "./timeline/sampler_layout.js";

const SAMPLER = "H3ContinuumSamplerV34";
const TIMELINE_SAMPLER = "H3ContinuumTimelineSampler";
const isSampler = (type) => type === SAMPLER || type === TIMELINE_SAMPLER;
const ASSEMBLER = "H3ContinuumAssembleSeamV34";
const BASE_REFERENCE_INPUTS = 3;
const MAX_REFERENCE_INPUTS = 8;
const REFERENCE_NAME = /^reference_image_(\d+)$/;
const LEGACY_CONTINUATION = "Guide / Motion Context";
const NATIVE_CONTINUATION = "Native Masked — exact continuation (Recommended)";
const CONTINUITY_AUTO = "Auto — conservative";
const CONTINUITY_BALANCED = "Balanced — 22 frames";
const CONTINUITY_FAST = "Fast — 5 frames";
const CONTINUITY_STRONG = "Strong — 39 frames";
const CONTINUITY_ALL = [
    CONTINUITY_AUTO,
    CONTINUITY_BALANCED,
    CONTINUITY_FAST,
    CONTINUITY_STRONG,
];
const TIMELINE_NATURAL = "Natural retained timeline (Refinement)";
const SETTINGS = {
    detailedReport: "H3Continuum.DetailedReport",
    developerDiagnostics: "H3Continuum.DeveloperDiagnostics",
    samplingPreview: "H3Continuum.SamplingPreview",
};

function settingValue(id, fallback) {
    try {
        return app.ui?.settings?.getSettingValue?.(id) ?? fallback;
    } catch {
        return fallback;
    }
}

function createProjectId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function findWidget(node, name) {
    return node.widgets?.find((widget) => widget.name === name);
}

function setWidgetVisible(widget, visible) {
    if (!widget) return;
    if (!widget.__h3ContinuumV34Original) {
        widget.__h3ContinuumV34Original = {
            type: widget.type,
            computeSize: widget.computeSize,
            hidden: widget.hidden,
            optionsHidden: widget.options?.hidden,
        };
    }
    widget.options ||= {};
    if (visible) {
        const original = widget.__h3ContinuumV34Original;
        widget.type = original.type;
        widget.computeSize = original.computeSize;
        widget.hidden = original.hidden;
        if (original.optionsHidden === undefined) delete widget.options.hidden;
        else widget.options.hidden = original.optionsHidden;
    } else {
        widget.hidden = true;
        widget.type = "converted-widget";
        widget.options.hidden = true;
        widget.computeSize = () => [0, -4];
    }
}

function linkedInput(node, names) {
    return node.inputs?.some((input) => names.includes(input.name) && input.link != null) ?? false;
}

function linkedOutput(node, name) {
    const output = node.outputs?.find((candidate) => candidate.name === name);
    return Array.isArray(output?.links) && output.links.length > 0;
}

function referenceIndex(input) {
    const match = REFERENCE_NAME.exec(String(input?.name ?? ""));
    if (!match) return 0;
    const index = Number(match[1]);
    return Number.isInteger(index) && index >= 1 && index <= MAX_REFERENCE_INPUTS ? index : 0;
}

function reconcileReferences(node) {
    if (node.__h3ContinuumV34RefUpdating) return;
    node.__h3ContinuumV34RefUpdating = true;
    try {
        const refs = new Map();
        let highestPresent = BASE_REFERENCE_INPUTS;
        let highestConnected = 0;
        for (const input of node.inputs || []) {
            const index = referenceIndex(input);
            if (!index) continue;
            refs.set(index, input);
            highestPresent = Math.max(highestPresent, index);
            if (input.link != null) highestConnected = Math.max(highestConnected, index);
        }
        const addThrough = (highest) => {
            for (let index = BASE_REFERENCE_INPUTS + 1; index <= highest; index += 1) {
                if (refs.has(index)) continue;
                node.addInput?.(`reference_image_${index}`, "IMAGE");
                const added = node.inputs?.[node.inputs.length - 1];
                if (added) refs.set(index, added);
            }
        };
        addThrough(highestPresent);
        const desiredHighest = Math.min(
            MAX_REFERENCE_INPUTS,
            Math.max(BASE_REFERENCE_INPUTS, highestConnected + 1),
        );
        addThrough(desiredHighest);
        for (let index = MAX_REFERENCE_INPUTS; index > desiredHighest; index -= 1) {
            const input = refs.get(index);
            if (!input || input.link != null) continue;
            const slot = node.inputs?.findIndex((candidate) => referenceIndex(candidate) === index) ?? -1;
            if (slot >= 0) node.removeInput?.(slot);
            refs.delete(index);
        }
    } finally {
        node.__h3ContinuumV34RefUpdating = false;
    }
}

function regenerateOptions(chunks) {
    const count = Math.max(1, Math.min(16, Number.parseInt(chunks, 10) || 1));
    return ["Auto", ...Array.from({ length: count }, (_, index) => `Chunk ${index + 1}`)];
}

function reconcileContinuity(node) {
    // Timeline guide coverage is scheduled per invocation. Runtime resolves
    // the native constraints from that coverage rather than a graph socket.
    if (node.comfyClass === TIMELINE_SAMPLER) return;
    const continuity = findWidget(node, "continuity");
    if (!continuity) return;
    const chunks = Number.parseInt(findWidget(node, "chunks")?.value, 10) || 1;
    const method = findWidget(node, "continuation_method")?.value;
    const audioContinuity = Boolean(findWidget(node, "audio_continuity")?.value);
    const generatedAudioExactAv = (
        chunks > 1
        && method === NATIVE_CONTINUATION
        && audioContinuity
        && !linkedInput(node, ["driving_audio"])
    );
    const values = generatedAudioExactAv
        ? [CONTINUITY_AUTO, CONTINUITY_STRONG]
        : CONTINUITY_ALL;
    continuity.options ||= {};
    continuity.options.values = values;
    if (!values.includes(continuity.value)) continuity.value = CONTINUITY_STRONG;
}

function refreshSampler(node) {
    const timeline = node.comfyClass === TIMELINE_SAMPLER;
    reconcileReferences(node);
    if (timeline) {
        orderSampler(node);
        refreshProjectWidgets(node);
    }
    reconcileContinuity(node);
    const project = findWidget(node, "project_id");
    if (!timeline && project && !String(project.value || "").trim()) project.value = createProjectId();

    const diagnostics = findWidget(node, "diagnostics");
    const debug = findWidget(node, "debug");
    const preview = findWidget(node, "show_preview");
    const strict = findWidget(node, "strict_compatibility");
    const emitRefine = findWidget(node, timeline ? "emit_refine_state" : "emit_refine_conditioning");
    if (diagnostics) diagnostics.value = settingValue(SETTINGS.detailedReport, false) ? "Detailed Report" : "Basic";
    if (debug) debug.value = Boolean(settingValue(SETTINGS.developerDiagnostics, false));
    if (preview) preview.value = Boolean(settingValue(SETTINGS.samplingPreview, true));
    if (strict) strict.value = false;
    if (emitRefine) emitRefine.value = linkedOutput(node, "refine_state");
    for (const widget of [diagnostics, debug, preview, strict, project, emitRefine]) setWidgetVisible(widget, false);

    const chunks = findWidget(node, "chunks");
    const regenerate = findWidget(node, timeline ? "regenerate_from" : "reroll_from_chunk");
    const storage = findWidget(node, "run_storage");
    const nonce = findWidget(node, "reroll_nonce");
    const runName = findWidget(node, "run_name");
    if (!timeline && chunks && regenerate) {
        regenerate.options ||= {};
        regenerate.options.values = regenerateOptions(chunks.value);
        if (!regenerate.options.values.includes(regenerate.value)) regenerate.value = "Auto";
    }
    const storageEnabled = storage?.value === "Save + Auto Resume";
    const explicitRegeneration = storageEnabled && (timeline ? Number(regenerate?.value) > 0 : regenerate?.value !== "Auto");
    setWidgetVisible(regenerate, storageEnabled);
    setWidgetVisible(runName, storageEnabled);
    setWidgetVisible(nonce, explicitRegeneration);
    setWidgetVisible(
        findWidget(node, "reference_size"),
        timeline || linkedInput(node, Array.from({ length: MAX_REFERENCE_INPUTS }, (_, index) => `reference_image_${index + 1}`)),
    );
    setWidgetVisible(
        findWidget(node, "video_reference_size"),
        timeline || linkedInput(node, ["reference_video_1"]),
    );
    if (timeline && node.computeSize && node.setSize) {
        const requiredSize = node.computeSize();
        node.setSize([Math.max(node.size[0], requiredSize[0], 420), requiredSize[1]]);
    }
    node.setDirtyCanvas?.(true, true);
    return project;
}

function refreshAssembler(node) {
    const exact = findWidget(node, "exact_total_duration");
    const timelineMode = findWidget(node, "timeline_mode");
    const diagnostics = findWidget(node, "diagnostics");
    if (exact) exact.value = timelineMode?.value !== TIMELINE_NATURAL;
    if (diagnostics) diagnostics.value = settingValue(SETTINGS.detailedReport, false) ? "Detailed Report" : "Basic";
    setWidgetVisible(exact, false);
    setWidgetVisible(diagnostics, false);
    node.setDirtyCanvas?.(true, true);
}

function serializedContinuationMethod(info) {
    const named = info?.widgets_values_named;
    if (named && typeof named === "object" && Object.hasOwn(named, "continuation_method")) {
        return named.continuation_method;
    }
    return undefined;
}

function migrateLegacySampler(node, info) {
    if (!info || node.__h3ContinuumV34ContinuationMigrated) return;
    const method = findWidget(node, "continuation_method");
    if (!method) return;
    if (serializedContinuationMethod(info) === undefined) {
        method.value = LEGACY_CONTINUATION;
    }
    node.__h3ContinuumV34ContinuationMigrated = true;
}

function install(node) {
    if (!node || node.__h3ContinuumV34Installed) return;
    const type = String(node.comfyClass || node.type || "");
    if (!isSampler(type) && type !== ASSEMBLER) return;
    node.__h3ContinuumV34Installed = true;
    if (type === TIMELINE_SAMPLER) {
        installProjectWidgets(node);
        orderSampler(node);
    }
    const refresh = () => {
        if (isSampler(type)) refreshSampler(node);
        else refreshAssembler(node);
    };
    const previousConnections = node.onConnectionsChange;
    node.onConnectionsChange = function (...args) {
        const result = previousConnections?.apply(this, args);
        queueMicrotask(refresh);
        return result;
    };
    const previousConfigure = node.onConfigure;
    node.onConfigure = function (...args) {
        const result = previousConfigure?.apply(this, args);
        if (type === SAMPLER) migrateLegacySampler(this, args[0]);
        queueMicrotask(refresh);
        return result;
    };
    for (const widgetName of [
        "chunks",
        "run_storage",
        "reroll_from_chunk",
        "regenerate_from",
        "continuation_method",
        "audio_continuity",
        "timeline_mode",
    ]) {
        const widget = findWidget(node, widgetName);
        if (!widget || widget.__h3ContinuumV34Callback) continue;
        const previous = widget.callback;
        widget.callback = function (value, ...args) {
            const result = previous?.call(this, value, ...args);
            queueMicrotask(refresh);
            return result;
        };
        widget.__h3ContinuumV34Callback = true;
    }
    queueMicrotask(refresh);
}

app.registerExtension({
    name: "H3Continuum.V34UI",
    nodeCreated(node) {
        install(node);
    },
    loadedGraphNode(node) {
        install(node);
    },
    afterConfigureGraph() {
        for (const node of app.graph?._nodes || []) install(node);
    },
    async beforeQueuePrompt(prompt) {
        const seen = new Set();
        for (const node of app.graph?._nodes || []) {
            const type = String(node.comfyClass || node.type || "");
            if (isSampler(type)) {
                const project = refreshSampler(node);
                let projectId = String(project?.value || "").trim();
                if (type === SAMPLER && (!projectId || seen.has(projectId))) {
                    projectId = createProjectId();
                    if (project) project.value = projectId;
                }
                seen.add(projectId);
                const apiNode = prompt.output?.[String(node.id)];
                if (apiNode?.inputs) {
                    apiNode.inputs.project_id = projectId;
                    apiNode.inputs.strict_compatibility = false;
                    apiNode.inputs.debug = Boolean(settingValue(SETTINGS.developerDiagnostics, false));
                    apiNode.inputs.show_preview = Boolean(settingValue(SETTINGS.samplingPreview, true));
                    apiNode.inputs.diagnostics = settingValue(SETTINGS.detailedReport, false)
                        ? "Detailed Report"
                        : "Basic";
                    apiNode.inputs[type === TIMELINE_SAMPLER ? "emit_refine_state" : "emit_refine_conditioning"] = linkedOutput(node, "refine_state");
                    const continuity = findWidget(node, "continuity");
                    if (continuity) apiNode.inputs.continuity = continuity.value;
                }
            } else if (type === ASSEMBLER) {
                refreshAssembler(node);
                const apiNode = prompt.output?.[String(node.id)];
                if (apiNode?.inputs) {
                    const timelineMode = findWidget(node, "timeline_mode");
                    apiNode.inputs.exact_total_duration = timelineMode?.value !== TIMELINE_NATURAL;
                    if (timelineMode) apiNode.inputs.timeline_mode = timelineMode.value;
                    apiNode.inputs.diagnostics = settingValue(SETTINGS.detailedReport, false)
                        ? "Detailed Report"
                        : "Basic";
                }
            }
        }
    },
});
