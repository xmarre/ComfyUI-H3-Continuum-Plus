"""Integrated N-chunk Continuum runtime.

V2 deliberately reuses the proven V1 continuation core. It changes execution
orchestration only: all text/image conditioning is prepared first, H3 sampling
runs for every chunk without intermediate VAE decoding, and decoding/assembly
happens after the sampling phase.
"""
from __future__ import annotations
import copy, hashlib, logging
from typing import Any
import torch
from ..compatibility import accelerator_summary, check_comfy_h3_runtime
from ..constants import (
    CONTINUITY_FRAMES,
    DIAGNOSTICS_FULL, DIAGNOSTICS_OFF, DIAGNOSTICS_OPTIONS, FPS,
    CONTINUUM_ACTUAL_PREFIX_STEPS, SEAM_CORRECTION_AUTO, SEAM_CORRECTION_OFF,
    SEAM_CORRECTION_OPTIONS, normalize_diagnostics_mode,
)
from ..continuation import POLICY_REPLACE, prepare_conditioning
from ..driving_audio import (
    attach_driving_audio,
    combine_driving_audio_identity,
    encode_driving_audio,
    slice_driving_audio_latent,
)
from ..masked_continuation import (
    CONTINUATION_GUIDE,
    CONTINUATION_NATIVE_MASKED,
    NATIVE_MASK_CONTRACT_VERSION,
    apply_native_masked_continuation,
    choose_continuation_context_frames,
    continuation_storage_plan,
    current_continuation_method,
    prepare_masked_conditioning,
    require_native_mask_support,
    stored_plan_matches_method,
)
from ..model_patch import clone_model_for_chunk
from ..state import assert_context_unchanged, context_fingerprint, make_plan, select_context, validate_state
from ..temporal import (
    align_frame_count_up,
    audio_latent_t,
    context_slots,
    largest_context_capacity,
    make_extension_shape,
    make_extension_shape_at_least,
    video_latent_t,
)
from ..version import PACKAGE_VERSION
from .decoder import decode_sequence, decode_sequence_with_seam, enforce_total_frames
from .context_diagnostics import ContextDiagnosticsTracker
from .h3_builder import attach_keyframes, empty_h3_latent, encode_identity_latents, encode_prompt_conditioning, prepare_identity_assets
from .physical_prompts import (
    LEGACY_COMPILER_VERSION,
    PHYSICAL_COMPILER_VERSION,
    legacy_entry_can_reuse,
    make_physical_sample_descriptor,
    physical_metadata_matches,
    physical_prompt_compiler_enabled,
    physical_timeline_video_enabled,
)
from .physical_runtime import (
    build_presentation_contract,
    conditioning_telemetry,
    encode_physical_prompt_conditioning,
)
from .physical_sequence import (
    compile_active_metadata,
    make_normal_descriptor,
    resolve_normal_geometry,
    video_presentation_contract,
)
from .prompts import prompt_plan_report, validate_prompt_plan
from .sampling import latent_from_cpu, latent_to_cpu, sample_chunk
from .seeds import derive_chunk_seed
from .session import (
    SessionValidationError, entry_to_state, make_chunk_entry, make_session, model_fingerprint,
    session_summary, validate_chunk_entry, validate_session,
)
LOG=logging.getLogger("h3_continuum_join")

def _prompt_routing_receipt(plan: dict[str, Any], *, physical_candidate: bool) -> dict[str, Any]:
    """Return bounded prompt-routing provenance without exposing prompt text."""

    source = plan.get("source") or {}
    source_kind = str(source.get("kind", "unknown"))
    return {
        "requested_prompt_mode": str(source.get("requested_mode", plan.get("mode", "unknown"))),
        "resolved_plan_mode": str(plan.get("mode", "unknown")),
        "source_kind": source_kind,
        "physical_compiler_enabled": bool(physical_candidate),
        "physical_compiler_eligible": bool(physical_candidate and source_kind == "timeline"),
        "source_digest": str(source.get("source_digest", "")),
    }


def _log_prompt_routing_receipt(plan: dict[str, Any], *, physical_candidate: bool) -> dict[str, Any]:
    receipt = _prompt_routing_receipt(plan, physical_candidate=physical_candidate)
    LOG.info(
        "H3C-PT209 prompt-routing receipt requested_mode=%r resolved_mode=%r "
        "source_kind=%s physical_compiler_enabled=%s physical_compiler_eligible=%s source_digest=%s",
        receipt["requested_prompt_mode"],
        receipt["resolved_plan_mode"],
        receipt["source_kind"],
        receipt["physical_compiler_enabled"],
        receipt["physical_compiler_eligible"],
        receipt["source_digest"],
    )
    return receipt


class SequenceRuntimeError(RuntimeError): pass

def _record_context_diagnostics(*,tracker,reports,state,continuity,reused,continuation_method=CONTINUATION_GUIDE,audio_continuity=True,driving_audio_active=False):
    if tracker is None: return
    try:
        context_frames,_,_=choose_continuation_context_frames(method=continuation_method,continuity=continuity,state=state,audio_continuity=bool(audio_continuity),driving_audio_active=bool(driving_audio_active))
        video_context,_,_=select_context(state,context_frames,include_audio=False)
        reports.append(tracker.observe(video_context,source_chunk=int(state["clip_index"]),context_frames=context_frames,reused=bool(reused)))
    except Exception as exc:
        reports.append(f"context diagnostics source chunk {state.get('clip_index','?')}: unavailable ({exc})")

def _check_decode_memory_budget(*,width,height,chunks,chunk_seconds):
    target_frames=max(1,int(round(chunks*chunk_seconds*FPS))); max_chunk_frames=max(5,int(round(chunk_seconds*FPS))+39)
    image_bytes=target_frames*width*height*3*4; transient_bytes=max_chunk_frames*width*height*3*4
    required_bytes=image_bytes+int(transient_bytes*1.5)+1*1024**3; estimate_gib=required_bytes/1024**3; available_gib=None
    try:
        import psutil
        available=int(psutil.virtual_memory().available); available_gib=available/1024**3
    except ImportError: pass
    return estimate_gib,available_gib

def _clone_entry_for_reuse(entry):
    entry=validate_chunk_entry(entry); result=dict(entry); result["plan"]=copy.deepcopy(entry["plan"]); result["reused"]=True; return result

def _preserved_prefix(*,session,prompt_hashes,chunks,reroll_from_chunk,width,height,chunk_seconds,identity_hash,first_frame_hash,last_frame_hash,continuation_method=CONTINUATION_GUIDE):
    if session is None: return [],[]
    notes=[]
    try: session=validate_session(session)
    except SessionValidationError as exc: return [],[f"saved session was ignored; generated a fresh run ({exc})"]
    if int(session["width"])!=int(width) or int(session["height"])!=int(height): return [],["saved session resolution differs; generated a fresh run"]
    if abs(float(session["chunk_seconds"])-float(chunk_seconds))>1e-6: return [],["saved session chunk duration differs; generated a fresh run"]
    if str(session.get("identity_hash","none"))!=str(identity_hash): return [],["saved session identity differs; generated a fresh run"]
    saved_settings=session.get("settings") or {}
    if str(saved_settings.get("first_frame_hash","none"))!=str(first_frame_hash or "none"):
        return [],["saved session First Frame differs; generated a fresh run"]
    last_frame_changed=str(saved_settings.get("last_frame_hash","none"))!=str(last_frame_hash or "none")
    if reroll_from_chunk<0 or reroll_from_chunk>chunks: raise SessionValidationError("reroll_from_chunk must be 0 or a valid one-based chunk index")
    limit=min(len(session["chunks"]),chunks)
    if reroll_from_chunk>0: limit=min(limit,reroll_from_chunk-1)
    if last_frame_changed:
        limit=min(limit,max(0,chunks-1))
        notes.append("saved session Last Frame differs; regenerated the final logical chunk")
    preserved=[]; retained_frames=0
    for index in range(limit):
        try: entry=validate_chunk_entry(session["chunks"][index])
        except SessionValidationError as exc:
            notes.append(f"session reuse stopped before chunk {index+1}: stored chunk was rejected ({exc})")
            break
        if not stored_plan_matches_method(entry["plan"],continuation_method,chunk_number=index+1):
            notes.append(f"session reuse stopped before chunk {index+1}: continuation method or mask contract changed")
            break
        if entry["prompt_hash"]!=prompt_hashes[index]: notes.append(f"session reuse stopped before chunk {index+1}: prompt changed"); break
        candidate_retained=retained_frames+int(entry["plan"]["net_frames"])
        if index==chunks-1:
            target_frames=int(round((index+1)*float(chunk_seconds)*FPS))
            if candidate_retained<target_frames:
                notes.append(f"session reuse stopped before chunk {index+1}: stored sequence retained {candidate_retained} frames for {target_frames}-frame target")
                break
        preserved.append(_clone_entry_for_reuse(entry)); retained_frames=candidate_retained
    if reroll_from_chunk==0 and len(preserved)==min(len(session["chunks"]),chunks):
        notes.append(f"resuming after accepted chunk {len(session['chunks'])}" if len(session["chunks"])<chunks else "all requested chunks reused from the session")
    elif reroll_from_chunk>0: notes.append(f"preserved chunks 1-{len(preserved)}; regenerated from chunk {reroll_from_chunk}")
    return preserved,notes

def _conditioning_cache(*,clip,prompts,assets,final_has_last_frame,reference_assets=None,reference_audio_assets=None,timeline_video_assets=None,include_first_frame=True,cache=None):
    # Retained as the PR #20 compatibility helper and direct unit-test surface.
    # The runtime path below now resolves physical geometry before calling the
    # equivalent cache-aware encoder in physical_runtime.py.
    cache={} if cache is None else cache; final_index=len(prompts)-1
    for index,prompt in enumerate(prompts):
        include_first=bool(include_first_frame and assets.first_image is not None); include_last=bool(final_has_last_frame and index==final_index); key=(prompt,include_first,include_last)
        if key in cache: continue
        first_image=assets.first_image if include_first else None
        last_image=assets.last_image if include_last else None
        if reference_assets is not None:
            from ..reference import encode_reference_prompt
            cache[key]=encode_reference_prompt(clip,prompt,reference_assets,first_image=first_image,last_image=last_image,reference_audio_assets=reference_audio_assets,timeline_video_assets=timeline_video_assets)
        else:
            cache[key]=encode_prompt_conditioning(clip,prompt,first_image=first_image,last_image=last_image,reference_audio_assets=reference_audio_assets,timeline_video_assets=timeline_video_assets)
    return cache


TERMINAL_MERGE_CONTRACT_VERSION = 1
TERMINAL_MERGE_STRATEGY = "fl2va_terminal_merge_v1"
TERMINAL_MERGE_CHUNK_SECONDS = 5.0
TERMINAL_MERGE_CONTEXT_FRAMES = 22
TERMINAL_PROMPT_POLICY_SHARED = "shared_prompt_v1"
TERMINAL_PROMPT_POLICY_TIMELINE = "paired_timeline_v1"
TERMINAL_PROMPT_POLICY_PHYSICAL = "physical_timeline_v1"


def _terminal_flf_merge_enabled(
    *,
    latent_only: bool,
    initial_state_present: bool,
    multi_chunk_flf: bool,
    chunks: int,
    chunk_seconds: float,
    timeline_video_source,
    continuation_method: str,
    continuity: str,
) -> bool:
    """Use only the upstream terminal paths whose temporal contract is proven here."""

    if not (
        latent_only
        and multi_chunk_flf
        and int(chunks) >= 2
        and abs(float(chunk_seconds) - TERMINAL_MERGE_CHUNK_SECONDS) <= 1e-6
        and timeline_video_source is None
    ):
        return False
    if int(chunks) == 2:
        # No prior chunk exists, so this is exactly one ordinary Core 10-second
        # FL2VA sample under either Continuum method.
        return not bool(initial_state_present)
    # The validated long-terminal contract uses a 22-frame Guide prefix. Native
    # Masked generated-audio continuation requires 39 frames / 65 audio steps.
    return (
        continuation_method == CONTINUATION_GUIDE
        and CONTINUITY_FRAMES.get(str(continuity)) == TERMINAL_MERGE_CONTEXT_FRAMES
    )


def _terminal_pair_prompt(
    prompts: list[str], *, pair_start: int, chunk_seconds: float
) -> tuple[str, str]:
    first = str(prompts[int(pair_start)])
    second = str(prompts[int(pair_start) + 1])
    if first == second:
        return first, TERMINAL_PROMPT_POLICY_SHARED
    split = f"{float(chunk_seconds):g}"
    stop = f"{float(chunk_seconds) * 2.0:g}"
    return (
        f"[0-{split}s]\n{first}\n\n[{split}-{stop}s]\n{second}",
        TERMINAL_PROMPT_POLICY_TIMELINE,
    )


def _terminal_storage_plan(
    prompt_plan: dict[str, Any], *, enabled: bool, prompt_policy: str | None
) -> dict[str, Any]:
    if not enabled:
        return prompt_plan
    result=dict(prompt_plan)
    hashes=list(prompt_plan["hashes"])
    salt=(
        f"terminal_merge={TERMINAL_MERGE_STRATEGY}\0"
        f"contract={TERMINAL_MERGE_CONTRACT_VERSION}\0"
        f"prompt_policy={prompt_policy or ''}"
    )
    for index in range(max(0,len(hashes)-2),len(hashes)):
        payload=f"{hashes[index]}\0{salt}".encode("utf-8")
        hashes[index]=f"h3c-terminal-v{TERMINAL_MERGE_CONTRACT_VERSION}:"+hashlib.sha256(payload).hexdigest()
    result["hashes"]=hashes
    return result


def _terminal_sampling_plan(*,chunks:int,completed:int,merge_enabled:bool)->tuple[tuple[int,...],bool]:
    if completed>=int(chunks): return (),False
    if not merge_enabled: return tuple(range(completed,int(chunks))),False
    pair_start=int(chunks)-2
    if completed>pair_start: raise SequenceRuntimeError("terminal merged pair cannot resume from only one logical half")
    return tuple(range(completed,pair_start)),True


def _terminal_physical_seed_plan(*,base_seed:int,physical_window_start_index:int,reroll_nonce:int)->dict[str,Any]:
    start_index=int(physical_window_start_index); nonce=int(reroll_nonce)
    if start_index<0: raise ValueError("physical_window_start_index must be non-negative")
    if nonce<0: raise ValueError("reroll_nonce must be non-negative")
    physical_seed=int(base_seed) if start_index==0 and nonce==0 else derive_chunk_seed(base_seed,start_index,nonce)
    return {"physical_seed":physical_seed,"logical_entry_seeds":(physical_seed,physical_seed)}


def _terminal_pair_contract(*,initial_pair:bool,chunk_seconds:float)->dict[str,Any]:
    if abs(float(chunk_seconds)-TERMINAL_MERGE_CHUNK_SECONDS)>1e-6:
        raise SequenceRuntimeError("terminal merged sampling requires 5-second logical chunks")
    initial_frames=align_frame_count_up(int(round(float(chunk_seconds)*FPS)))
    extension=make_extension_shape(TERMINAL_MERGE_CONTEXT_FRAMES,float(chunk_seconds))
    if initial_pair:
        physical_frames=align_frame_count_up(int(round(2.0*float(chunk_seconds)*FPS)))
        logical_frames=(initial_frames,extension.total_frames); logical_trims=(0,TERMINAL_MERGE_CONTEXT_FRAMES)
        first_video_stop=video_latent_t(initial_frames); first_audio_stop=audio_latent_t(initial_frames)
    else:
        physical_frames=make_extension_shape(TERMINAL_MERGE_CONTEXT_FRAMES,2.0*float(chunk_seconds)).total_frames
        logical_frames=(extension.total_frames,extension.total_frames); logical_trims=(TERMINAL_MERGE_CONTEXT_FRAMES,TERMINAL_MERGE_CONTEXT_FRAMES)
        first_video_stop=video_latent_t(extension.total_frames); first_audio_stop=audio_latent_t(extension.total_frames)
    physical_video_stop=video_latent_t(physical_frames); physical_audio_stop=audio_latent_t(physical_frames)
    video_overlap=context_slots(TERMINAL_MERGE_CONTEXT_FRAMES); audio_overlap=audio_latent_t(TERMINAL_MERGE_CONTEXT_FRAMES)
    video_slices=((0,first_video_stop),(first_video_stop-video_overlap,physical_video_stop))
    audio_slices=((0,first_audio_stop),(first_audio_stop-audio_overlap,physical_audio_stop))
    for index in range(2):
        if video_slices[index][1]-video_slices[index][0]!=video_latent_t(logical_frames[index]):
            raise SequenceRuntimeError("derived terminal video split does not match its logical frame count")
        if audio_slices[index][1]-audio_slices[index][0]!=audio_latent_t(logical_frames[index]):
            raise SequenceRuntimeError("derived terminal audio split does not match its logical frame count")
    return {"initial_pair":bool(initial_pair),"physical_frames":int(physical_frames),"physical_context_frames":0 if initial_pair else TERMINAL_MERGE_CONTEXT_FRAMES,"logical_frames":logical_frames,"logical_trims":logical_trims,"video_slices":video_slices,"audio_slices":audio_slices}


def _split_terminal_merged_latents(video:torch.Tensor,audio:torch.Tensor,contract:dict[str,Any]):
    expected_video=video_latent_t(int(contract["physical_frames"])); expected_audio=audio_latent_t(int(contract["physical_frames"]))
    if int(video.shape[2])!=expected_video or int(audio.shape[-1])!=expected_audio:
        raise SequenceRuntimeError(f"terminal merged sampler output does not match the physical AV grid: video T={video.shape[2]} (expected {expected_video}), audio T={audio.shape[-1]} (expected {expected_audio})")
    parts=[]
    for video_range,audio_range in zip(contract["video_slices"],contract["audio_slices"]):
        parts.append((video[:,:,video_range[0]:video_range[1],...].contiguous(),audio[...,audio_range[0]:audio_range[1]].contiguous()))
    return parts[0],parts[1]


def _mark_terminal_plan(plan:dict[str,Any],*,contract:dict[str,Any],role:int)->dict[str,Any]:
    result=dict(plan)
    result["terminal_merge"]={
        "version":TERMINAL_MERGE_CONTRACT_VERSION,
        "strategy":TERMINAL_MERGE_STRATEGY,
        "role":int(role),
        "physical_frames":int(contract["physical_frames"]),
        "physical_context_frames":int(contract["physical_context_frames"]),
        "video_slice":list(contract["video_slices"][role]),
        "audio_slice":list(contract["audio_slices"][role]),
    }
    return result


def terminal_entries_match_contract(entries:list[dict[str,Any]],*,chunk_seconds:float)->bool:
    if len(entries)<2: return False
    try:
        contract=_terminal_pair_contract(initial_pair=len(entries)==2,chunk_seconds=chunk_seconds)
        for role,entry in enumerate(entries[-2:]):
            marker=(entry.get("plan") or {}).get("terminal_merge")
            if not isinstance(marker,dict): return False
            expected={"version":TERMINAL_MERGE_CONTRACT_VERSION,"strategy":TERMINAL_MERGE_STRATEGY,"role":role,"physical_frames":int(contract["physical_frames"]),"physical_context_frames":int(contract["physical_context_frames"]),"video_slice":list(contract["video_slices"][role]),"audio_slice":list(contract["audio_slices"][role])}
            if marker!=expected: return False
        return True
    except (KeyError,TypeError,ValueError,SequenceRuntimeError):
        return False


def _atomic_terminal_prefix(preserved:list[dict[str,Any]],*,chunks:int,reroll_from_chunk:int,chunk_seconds:float)->tuple[list[dict[str,Any]],bool]:
    pair_start=int(chunks)-2
    reroll_hits_pair=int(reroll_from_chunk) in (int(chunks)-1,int(chunks))
    partial_pair=pair_start<len(preserved)<int(chunks)
    stale_complete_pair=len(preserved)==int(chunks) and not terminal_entries_match_contract(preserved,chunk_seconds=chunk_seconds)
    if (reroll_hits_pair or partial_pair or stale_complete_pair) and len(preserved)>pair_start:
        return preserved[:pair_start],True
    return preserved,False


def _remove_inactive_terminal_prefix(preserved:list[dict[str,Any]])->tuple[list[dict[str,Any]],bool]:
    for index,entry in enumerate(preserved):
        if isinstance((entry.get("plan") or {}).get("terminal_merge"),dict):
            return preserved[:index],True
    return preserved,False


def _terminal_descriptor(
    *,
    pair_start:int,
    chunks:int,
    retained_before:int,
    contract:dict[str,Any],
    continuation_method:str,
    terminal_prompt_policy:str|None,
    assets:Any,
    initial_pair:bool,
    reference_assets=None,
    reference_audio_source=None,
    reference_video_source=None,
    target_duration_frames:int,
):
    presentation=build_presentation_contract(
        assets=assets,
        include_first=bool(initial_pair),
        include_last=True,
        reference_assets=reference_assets,
        reference_audio_source=reference_audio_source,
        video_presentation=video_presentation_contract(
            reference_video_source=reference_video_source,
            timeline_video_source=None,
            logical_index=pair_start,
        ),
    )
    terminal_identity={
        "version":TERMINAL_MERGE_CONTRACT_VERSION,
        "strategy":TERMINAL_MERGE_STRATEGY,
        "prompt_policy":terminal_prompt_policy,
        "initial_pair":bool(contract["initial_pair"]),
        "logical_frames":list(contract["logical_frames"]),
        "logical_trims":list(contract["logical_trims"]),
        "video_slices":[list(value) for value in contract["video_slices"]],
        "audio_slices":[list(value) for value in contract["audio_slices"]],
    }
    return make_physical_sample_descriptor(
        group_id=f"terminal:{pair_start+1}-{chunks}",
        logical_indices=(pair_start,pair_start+1),
        retained_before=int(retained_before),
        context_frames=int(contract["physical_context_frames"]),
        total_frames=int(contract["physical_frames"]),
        target_duration_frames=int(target_duration_frames),
        continuation_method=str(continuation_method),
        initial_state_origin="sequence",
        include_first=bool(initial_pair),
        include_last=True,
        presentation_contract=presentation,
        exact_protected=False,
        guided_overlap=(not initial_pair and int(contract["physical_context_frames"])>0),
        terminal_contract=terminal_identity,
    )


def _physical_reuse_prefix(
    preserved:list[dict[str,Any]],
    *,
    prompt_plan:dict[str,Any],
    prompts:list[str],
    chunks:int,
    chunk_seconds:float,
    width:int,
    height:int,
    continuity:str,
    continuation_method:str,
    audio_continuity:bool,
    driving_audio_active:bool,
    debug:bool,
    initial_frame_count:int,
    assets:Any,
    reference_assets=None,
    reference_audio_source=None,
    reference_video_source=None,
    timeline_video_source=None,
    initial_state_external:bool=False,
    terminal_merge_enabled:bool=False,
    terminal_prompt:str|None=None,
    terminal_prompt_policy:str|None=None,
)->tuple[list[dict[str,Any]],list[str]]:
    """Second-phase sequential reuse predicate for Session and Run Storage.

    The static candidate list has already passed storage/session integrity checks.
    Entries carrying physical metadata are authenticated against the currently
    selected compiler even when the current execution is the legacy control. This
    prevents a prior physical-Timeline candidate from silently contaminating a
    later legacy comparison. Metadata-free legacy entries retain their historical
    reuse behavior while the physical compiler is disabled. The first physical
    mismatch terminates reuse.
    """
    if not preserved:
        return preserved,[]
    candidate_enabled=physical_prompt_compiler_enabled()
    has_physical_metadata=any(
        isinstance((entry.get("plan") or {}).get("physical_prompt"),dict)
        for entry in preserved
    )
    if not candidate_enabled and not has_physical_metadata:
        return preserved,[]
    notes=[]; accepted=[]; retained=0; previous_state=None
    target_duration_frames=int(round(int(chunks)*float(chunk_seconds)*FPS))
    source_kind=str((prompt_plan.get("source") or {}).get("kind","legacy_logical"))
    pair_start=int(chunks)-2 if terminal_merge_enabled else int(chunks)
    index=0
    while index<len(preserved):
        if terminal_merge_enabled and index>=pair_start:
            if len(preserved)<int(chunks):
                notes.append(f"physical reuse stopped before chunk {pair_start+1}: terminal physical pair is incomplete")
                break
            pair=preserved[pair_start:chunks]
            if len(pair)!=2 or not terminal_entries_match_contract(pair,chunk_seconds=chunk_seconds):
                notes.append(f"physical reuse stopped before chunk {pair_start+1}: terminal pair contract differs")
                break
            pair_has_physical=any(
                isinstance((entry.get("plan") or {}).get("physical_prompt"),dict)
                for entry in pair
            )
            if not candidate_enabled and not pair_has_physical:
                accepted.extend(pair)
                retained+=sum(int(entry["plan"]["net_frames"]) for entry in pair)
                index+=2
                continue
            initial_pair=previous_state is None
            contract=_terminal_pair_contract(initial_pair=initial_pair,chunk_seconds=chunk_seconds)
            descriptor=_terminal_descriptor(
                pair_start=pair_start,chunks=chunks,retained_before=retained,contract=contract,
                continuation_method=continuation_method,terminal_prompt_policy=terminal_prompt_policy,
                assets=assets,initial_pair=initial_pair,reference_assets=reference_assets,
                reference_audio_source=reference_audio_source,reference_video_source=reference_video_source,
                target_duration_frames=target_duration_frames,
            )
            _,expected=compile_active_metadata(prompt_plan=prompt_plan,descriptor=descriptor,legacy_text=str(terminal_prompt or ""))
            if not all(physical_metadata_matches((entry.get("plan") or {}).get("physical_prompt"),expected) for entry in pair):
                notes.append(f"physical reuse stopped before chunk {pair_start+1}: terminal physical conditioning identity differs")
                break
            accepted.extend(pair)
            retained+=sum(int(entry["plan"]["net_frames"]) for entry in pair)
            index+=2
            continue
        entry=preserved[index]
        stored=(entry.get("plan") or {}).get("physical_prompt")
        if stored is None and not candidate_enabled:
            # Legacy Session-1 / Storage-2 data already passed the historical
            # static reuse checks. Preserve that behavior exactly; only entries
            # that claim a physical identity require cross-mode authentication.
            accepted.append(entry)
            retained+=int(entry["plan"]["net_frames"])
            previous_state=entry_to_state(entry)
            index+=1
            continue
        geometry=resolve_normal_geometry(
            previous_state=previous_state,sequence_index=index,chunks=chunks,chunk_seconds=chunk_seconds,
            retained_frames=retained,width=width,height=height,continuity=continuity,
            continuation_method=continuation_method,audio_continuity=audio_continuity,
            driving_audio_active=driving_audio_active,debug=debug,initial_frame_count=initial_frame_count,
        )
        include_first=previous_state is None
        include_last=bool(assets.last_image is not None and geometry.is_final)
        descriptor=make_normal_descriptor(
            geometry=geometry,retained_before=retained,target_duration_frames=target_duration_frames,
            continuation_method=continuation_method,initial_state_external=initial_state_external,
            assets=assets,include_first=include_first,include_last=include_last,
            reference_assets=reference_assets,reference_audio_source=reference_audio_source,
            reference_video_source=reference_video_source,timeline_video_source=timeline_video_source,
        )
        compiled,expected=compile_active_metadata(prompt_plan=prompt_plan,descriptor=descriptor,legacy_text=prompts[index])
        geometry_matches=(
            int(entry["plan"].get("total_frames",-1))==geometry.total_frames
            and int(entry["plan"].get("trim_frames",-1))==geometry.context_frames
        )
        matches=geometry_matches and physical_metadata_matches(stored,expected)
        if candidate_enabled and not matches and stored is None and geometry_matches:
            matches=legacy_entry_can_reuse(entry=entry,descriptor=descriptor,compiled=compiled,source_kind=source_kind)
            if matches:
                notes.append(f"chunk {index+1}: accepted conservative schema-1 initial legacy conditioning adapter")
        if not matches:
            notes.append(f"physical reuse stopped before chunk {index+1}: physical descriptor or conditioning identity differs")
            break
        accepted.append(entry)
        retained+=geometry.net_frames
        previous_state=entry_to_state(entry)
        index+=1
    if len(accepted)<len(preserved):
        notes.append("later stored chunks were not considered after the first physical reuse mismatch")
    return accepted,notes


def _physical_settings(entries:list[dict[str,Any]],plan:dict[str,Any])->dict[str,Any]:
    first=(entries[0].get("plan") or {}).get("physical_prompt") if entries else None
    compiled=(first or {}).get("compiled") or {}
    return {
        "contract_version":1,
        "compiler_version":str(compiled.get("compiler_version",LEGACY_COMPILER_VERSION)),
        "candidate_enabled":bool(physical_prompt_compiler_enabled()),
        "timeline_video_physical_enabled":bool(physical_timeline_video_enabled()),
        "prompt_source_digest":str((plan.get("source") or {}).get("source_digest","")),
    }


def run_sequence(*,model:Any,clip:Any,video_vae:Any,audio_vae:Any,sampler:Any,sigmas:torch.Tensor,first_frame:torch.Tensor|None,last_frame:torch.Tensor|None,prompt_plan:dict[str,Any],width:int,height:int,continuity:str,base_seed:int,audio_continuity:bool,exact_total_duration:bool,diagnostics_mode:str,reroll_from_chunk:int,reroll_nonce:int,strict_compatibility:bool,debug:bool,seam_correction:str=SEAM_CORRECTION_OFF,enable_preview:bool=True,session:dict[str,Any]|None=None,initial_state:dict[str,Any]|None=None,latent_only:bool=False,reference_assets=None,reference_audio_source=None,reference_audio_vae=None,driving_audio_source=None,driving_audio_vae=None,reference_video_source=None,timeline_video_source=None):
    from ..conditioning import detect_conditioning_mode, conditioning_mode_label
    from ..run_storage import get_active_run_storage
    storage_controller=get_active_run_storage()
    continuation_method=current_continuation_method()
    diagnostics_mode=normalize_diagnostics_mode(diagnostics_mode); plan=validate_prompt_plan(prompt_plan); chunks=int(plan["chunks"]); chunk_seconds=float(plan["chunk_seconds"]); prompts=list(plan["prompts"]); prompt_hashes=list(plan["hashes"]); width,height=int(width),int(height)
    physical_candidate=physical_prompt_compiler_enabled()
    _log_prompt_routing_receipt(plan, physical_candidate=physical_candidate)
    if continuation_method==CONTINUATION_NATIVE_MASKED: require_native_mask_support()
    # Legacy workflow input only. Runtime compatibility is advisory in V3.4.
    strict_compatibility=False
    if width<=0 or height<=0 or width%32 or height%32: raise SequenceRuntimeError("width and height must be positive multiples of 32")
    if session is not None and initial_state is not None:
        LOG.warning("Both session and initial_state were supplied; using the session and ignoring initial_state")
        initial_state=None
    if diagnostics_mode not in DIAGNOSTICS_OPTIONS: raise SequenceRuntimeError(f"unknown diagnostics mode: {diagnostics_mode!r}")
    if seam_correction not in SEAM_CORRECTION_OPTIONS: raise SequenceRuntimeError(f"unknown seam correction mode: {seam_correction!r}")
    if not 0<=int(reroll_from_chunk)<=chunks: raise SequenceRuntimeError("reroll_from_chunk must be 0 or a valid one-based chunk index")
    if initial_state is not None and int(reroll_from_chunk) not in (0,1): raise SequenceRuntimeError("with initial_state, reroll_from_chunk can only be 0 or 1")
    try: conditioning_mode=detect_conditioning_mode(first_frame=first_frame,last_frame=last_frame,reference_assets=reference_assets)
    except ValueError as exc: raise SequenceRuntimeError(str(exc)) from exc
    reference_warning=""
    if reference_assets is not None:
        from ..reference import validate_reference_prompts
        reference_warning=validate_reference_prompts(prompts,reference_assets.count)
    from ..reference_audio import (
        combine_reference_audio_identity,
        validate_reference_audio_prompts,
    )
    reference_audio_warning=validate_reference_audio_prompts(prompts,reference_audio_source)
    from ..reference_video import (
        combine_reference_video_identity,
        validate_reference_video_prompts,
    )
    reference_video_warning=validate_reference_video_prompts(prompts,reference_video_source)
    from ..timeline_video import (
        combine_timeline_video_identity,
        validate_timeline_video_prompts,
    )
    timeline_video_warning=validate_timeline_video_prompts(prompts,timeline_video_source)
    decode_estimate_gib=0.0; available_ram_gib=None
    if not latent_only: decode_estimate_gib,available_ram_gib=_check_decode_memory_budget(width=width,height=height,chunks=chunks,chunk_seconds=chunk_seconds)
    issues=check_comfy_h3_runtime()
    assets=prepare_identity_assets(video_vae,width=width,height=height,first_frame=first_frame,last_frame=last_frame,encode_latents=False)
    multi_chunk_flf=bool(chunks>=2 and first_frame is not None and last_frame is not None)
    terminal_merge_enabled=_terminal_flf_merge_enabled(
        latent_only=latent_only,
        initial_state_present=initial_state is not None,
        multi_chunk_flf=multi_chunk_flf,
        chunks=chunks,
        chunk_seconds=chunk_seconds,
        timeline_video_source=timeline_video_source,
        continuation_method=continuation_method,
        continuity=continuity,
    )
    terminal_prompt=None; terminal_prompt_policy=None
    if terminal_merge_enabled:
        terminal_prompt,legacy_terminal_policy=_terminal_pair_prompt(prompts,pair_start=chunks-2,chunk_seconds=chunk_seconds)
        terminal_prompt_policy=(
            TERMINAL_PROMPT_POLICY_PHYSICAL
            if physical_candidate and str((plan.get("source") or {}).get("kind"))=="timeline"
            else legacy_terminal_policy
        )
    visual_identity_hash=reference_assets.combined_hash if reference_assets is not None else assets.identity_hash
    sequence_identity_hash=combine_reference_audio_identity(visual_identity_hash,reference_audio_source)
    sequence_identity_hash=combine_driving_audio_identity(sequence_identity_hash,driving_audio_source)
    sequence_identity_hash=combine_reference_video_identity(sequence_identity_hash,reference_video_source)
    sequence_identity_hash=combine_timeline_video_identity(sequence_identity_hash,timeline_video_source)
    current_model_fingerprint=model_fingerprint(model,extra_wrapper_keys=("h3_continuum_join.apply_model.v1",))
    if storage_controller is not None:
        storage_plan=continuation_storage_plan(plan,continuation_method)
        storage_plan=dict(storage_plan)
        storage_plan["physical_prompt_policy"]=PHYSICAL_COMPILER_VERSION if physical_candidate else LEGACY_COMPILER_VERSION
        storage_plan["prompt_source_digest"]=str((plan.get("source") or {}).get("source_digest",""))
        storage_plan["timeline_video_adapter"]="physical_window_v1" if physical_timeline_video_enabled() else "legacy_nominal_chunk_v1"
        storage_plan=_terminal_storage_plan(storage_plan,enabled=terminal_merge_enabled,prompt_policy=terminal_prompt_policy)
        stored_session=storage_controller.prepare(model=model,model_fingerprint_value=current_model_fingerprint,clip=clip,video_vae=video_vae,sampler=sampler,sigmas=sigmas,prompt_plan=storage_plan,width=width,height=height,chunk_seconds=chunk_seconds,continuity=continuity,audio_continuity=audio_continuity,base_seed=base_seed,reroll_from_chunk=reroll_from_chunk,reroll_nonce=reroll_nonce,first_frame_hash=assets.first_frame_hash,last_frame_hash=assets.last_frame_hash,identity_hash=sequence_identity_hash,strict_compatibility=strict_compatibility,existing_session=session,reference_contract=reference_assets.contract if reference_assets is not None else None,conditioning_mode=conditioning_mode,reference_audio_contract=reference_audio_source.contract if reference_audio_source is not None else None,reference_audio_vae=reference_audio_vae,driving_audio_contract=driving_audio_source.contract if driving_audio_source is not None else None,driving_audio_vae=driving_audio_vae,reference_video_contract=reference_video_source.contract if reference_video_source is not None else None,timeline_video_contract=timeline_video_source.contract if timeline_video_source is not None else None)
        reroll_nonce=storage_controller.effective_reroll_nonce
        if stored_session is not None: session=stored_session
    accelerators=accelerator_summary(model)
    if "Continuum APPLY_MODEL wrapper installed" not in accelerators: accelerators+="; Continuum APPLY_MODEL wrapper installed"
    effective_reroll_from_chunk=0 if storage_controller is not None and session is not None and bool((session.get("settings") or {}).get("run_storage_validated_prefix")) else int(reroll_from_chunk)
    preserved,reuse_notes=_preserved_prefix(session=session,prompt_hashes=prompt_hashes,chunks=chunks,reroll_from_chunk=effective_reroll_from_chunk,width=width,height=height,chunk_seconds=chunk_seconds,identity_hash=sequence_identity_hash,first_frame_hash=assets.first_frame_hash,last_frame_hash=assets.last_frame_hash,continuation_method=continuation_method)
    if terminal_merge_enabled:
        preserved,atomic_reset=_atomic_terminal_prefix(preserved,chunks=chunks,reroll_from_chunk=int(reroll_from_chunk),chunk_seconds=chunk_seconds)
        if atomic_reset:
            reuse_notes.append("terminal merged pair is atomic; regenerated both final logical chunks")
            if storage_controller is not None: storage_controller.reused_count=len(preserved)
    else:
        preserved,inactive_reset=_remove_inactive_terminal_prefix(preserved)
        if inactive_reset:
            reuse_notes.append("saved terminal-merge entries do not match the active request; regenerated from the terminal pair boundary")
            if storage_controller is not None: storage_controller.reused_count=len(preserved)
    initial_frame_count=align_frame_count_up(int(round(chunk_seconds*FPS)))
    preserved,physical_reuse_notes=_physical_reuse_prefix(
        preserved,prompt_plan=plan,prompts=prompts,chunks=chunks,chunk_seconds=chunk_seconds,
        width=width,height=height,continuity=continuity,continuation_method=continuation_method,
        audio_continuity=audio_continuity,driving_audio_active=driving_audio_source is not None,
        debug=debug,initial_frame_count=initial_frame_count,assets=assets,
        reference_assets=reference_assets,reference_audio_source=reference_audio_source,
        reference_video_source=reference_video_source,timeline_video_source=timeline_video_source,
        initial_state_external=initial_state is not None,terminal_merge_enabled=terminal_merge_enabled,
        terminal_prompt=terminal_prompt,terminal_prompt_policy=terminal_prompt_policy,
    )
    reuse_notes.extend(physical_reuse_notes)
    if storage_controller is not None:
        storage_controller.reused_count=len(preserved)
    reuse_notes.insert(0,f"Continuation method: {continuation_method}."+(f" Native mask contract v{NATIVE_MASK_CONTRACT_VERSION}." if continuation_method==CONTINUATION_NATIVE_MASKED else ""))
    reuse_notes.insert(0,"Physical prompt transport: "+("candidate physical compiler enabled." if physical_candidate else "legacy nominal control enabled."))
    if timeline_video_source is not None:
        reuse_notes.insert(0,"Timeline Video adapter: "+("experimental physical-window adapter enabled." if physical_timeline_video_enabled() else "legacy nominal-chunk adapter retained."))
    if multi_chunk_flf:
        if terminal_merge_enabled:
            reuse_notes.insert(0,"FL2VA terminal merge: the final two 5-second logical chunks share one physical sample and external Core VAE decode group.")
        else:
            reuse_notes.insert(0,"FL2VA terminal merge: not eligible for this request; retained one physical sample per logical chunk.")
    if reference_assets is not None:
        reuse_notes.insert(0,f"Reference conditioning: {reference_assets.count} image(s), size={reference_assets.size_mode}; persistent across all chunks.")
        if reference_warning: reuse_notes.append(reference_warning)
    if reference_audio_source is not None:
        reuse_notes.insert(0,"Reference Audio 1 conditioning: persistent across all chunks.")
        if reference_audio_warning: reuse_notes.append(reference_audio_warning)
    if driving_audio_source is not None:
        reuse_notes.insert(0,"Driving Audio: absolute-time guide slices enabled; original audio is preserved for final output; generated-audio continuation is disabled while Driving Audio is authoritative.")
    if reference_video_source is not None:
        reuse_notes.insert(0,f"Video Reference conditioning: persistent across all chunks, 24 fps, resolved={reference_video_source.target_width}x{reference_video_source.target_height}, frames={reference_video_source.frame_count}.")
        if reference_video_warning: reuse_notes.append(reference_video_warning)
    if timeline_video_source is not None:
        reuse_notes.insert(0,f"Timeline Video conditioning: chunk-local slices, size={timeline_video_source.size_mode}, resolved={timeline_video_source.target_width}x{timeline_video_source.target_height}.")
        if timeline_video_warning: reuse_notes.append(timeline_video_warning)
    if session is not None:
        old_fingerprint=str(session.get("model_fingerprint",""))
        if old_fingerprint and old_fingerprint!=current_model_fingerprint: reuse_notes.append("model/accelerator fingerprint differs from the saved session; accepted chunks were kept")
    cache={}
    if len(preserved)<chunks:
        assets=encode_identity_latents(video_vae,assets)
        if reference_assets is not None:
            from ..reference import encode_reference_latents
            reference_assets=encode_reference_latents(video_vae,reference_assets)
        reference_audio_assets=None
        if reference_audio_source is not None:
            from ..reference_audio import encode_reference_audio
            reference_audio_assets=encode_reference_audio(reference_audio_vae,reference_audio_source)
        driving_audio_assets=encode_driving_audio(driving_audio_source,driving_audio_vae)
        reference_video_assets=None
        if reference_video_source is not None:
            from ..reference_video import encode_reference_video
            reference_video_assets=encode_reference_video(video_vae,reference_video_source)
    entries=preserved[:]; previous_state=None
    if entries:
        try: previous_state=entry_to_state(entries[-1])
        except (SessionValidationError, ValueError) as exc:
            reuse_notes.append(f"saved continuation state was rejected; generated a fresh run ({exc})")
            entries=[]; previous_state=None
    elif initial_state is not None:
        try:
            candidate=validate_state(initial_state)
            if int(candidate["width"])!=width or int(candidate["height"])!=height:
                reuse_notes.append("initial_state resolution differs; generated a fresh run")
            else: previous_state=candidate
        except ValueError as exc:
            reuse_notes.append(f"initial_state was rejected; generated a fresh run ({exc})")
    retained_frames=sum(int(entry["plan"]["net_frames"]) for entry in entries); sampling_reports=[]
    context_diagnostics=ContextDiagnosticsTracker() if bool(debug) else None
    if context_diagnostics is not None:
        if entries:
            for reused_entry in entries:
                _record_context_diagnostics(tracker=context_diagnostics,reports=sampling_reports,state=entry_to_state(reused_entry),continuity=continuity,reused=True,continuation_method=continuation_method,audio_continuity=audio_continuity,driving_audio_active=driving_audio_source is not None)
        elif previous_state is not None:
            _record_context_diagnostics(tracker=context_diagnostics,reports=sampling_reports,state=previous_state,continuity=continuity,reused=True,continuation_method=continuation_method,audio_continuity=audio_continuity,driving_audio_active=driving_audio_source is not None)
    normal_indices,terminal_merge_pending=_terminal_sampling_plan(chunks=chunks,completed=len(entries),merge_enabled=terminal_merge_enabled)
    target_duration_frames=int(round(chunks*chunk_seconds*FPS))
    for sequence_index in normal_indices:
        prompt=prompts[sequence_index]; prompt_hash_value=prompt_hashes[sequence_index]; effective_reroll_nonce=int(reroll_nonce) if int(reroll_from_chunk)>0 and sequence_index+1>=int(reroll_from_chunk) else 0; seed=derive_chunk_seed(base_seed,sequence_index,effective_reroll_nonce); video_context=None; audio_context=None; context_before=None
        geometry=resolve_normal_geometry(
            previous_state=previous_state,sequence_index=sequence_index,chunks=chunks,chunk_seconds=chunk_seconds,
            retained_frames=retained_frames,width=width,height=height,continuity=continuity,
            continuation_method=continuation_method,audio_continuity=audio_continuity,
            driving_audio_active=driving_audio_source is not None,debug=debug,initial_frame_count=initial_frame_count,
        )
        include_first=previous_state is None
        include_last=bool(last_frame is not None and geometry.is_final)
        descriptor=make_normal_descriptor(
            geometry=geometry,retained_before=retained_frames,target_duration_frames=target_duration_frames,
            continuation_method=continuation_method,initial_state_external=initial_state is not None and not entries,
            assets=assets,include_first=include_first,include_last=include_last,
            reference_assets=reference_assets,reference_audio_source=reference_audio_source,
            reference_video_source=reference_video_source,timeline_video_source=timeline_video_source,
        )
        timeline_video_assets=reference_video_assets
        chunk_cache=cache
        if timeline_video_source is not None:
            if physical_timeline_video_enabled():
                from ..timeline_video import encode_timeline_video_physical
                timeline_video_assets=encode_timeline_video_physical(video_vae,timeline_video_source,descriptor)
            else:
                from ..timeline_video import encode_timeline_video_chunk
                timeline_video_assets=encode_timeline_video_chunk(video_vae,timeline_video_source,sequence_index)
            chunk_cache={}
        base_conditioning,compiled,physical_meta,conditioning_key=encode_physical_prompt_conditioning(
            clip=clip,plan=plan,descriptor=descriptor,legacy_text=prompt,assets=assets,
            include_first=include_first,include_last=include_last,reference_assets=reference_assets,
            reference_audio_assets=reference_audio_assets,timeline_video_assets=timeline_video_assets,cache=chunk_cache,
        )
        token_telemetry=conditioning_telemetry(base_conditioning)
        chunk_plan=dict(geometry.plan); chunk_plan["physical_prompt"]=physical_meta
        latent=empty_h3_latent(width,height,geometry.total_frames)
        if not geometry.continuation:
            conditioning=attach_keyframes(base_conditioning,frame_count=geometry.total_frames,first_latent=assets.first_latent,last_latent=assets.last_latent if geometry.is_final else None)
        else:
            keyed_conditioning=attach_keyframes(base_conditioning,frame_count=geometry.total_frames,first_latent=None,last_latent=assets.last_latent if geometry.is_final else None)
            carry_generated_audio=bool(audio_continuity) and driving_audio_source is None
            video_context,audio_context,grid_offset=select_context(previous_state,geometry.context_frames,include_audio=carry_generated_audio); context_before=context_fingerprint(video_context,audio_context)
            if continuation_method==CONTINUATION_NATIVE_MASKED:
                latent=apply_native_masked_continuation(latent,video_context=video_context,audio_context=audio_context,context_frames=geometry.context_frames)
                conditioning=prepare_masked_conditioning(keyed_conditioning,context_frames=geometry.context_frames,new_frame_count=geometry.total_frames)
            else:
                conditioning=prepare_conditioning(keyed_conditioning,video_context=video_context,audio_context=audio_context,audio_grid_offset=grid_offset,context_frames=geometry.context_frames,new_frame_count=geometry.total_frames,first_frame_policy=POLICY_REPLACE,preserve_last_frame=True)
        driving_audio_latent=slice_driving_audio_latent(driving_audio_assets,cumulative_retained_before=retained_frames,total_frames=int(chunk_plan["total_frames"]),trim_frames=int(chunk_plan["trim_frames"]),fps=FPS)
        conditioning=attach_driving_audio(conditioning,driving_audio_latent)
        chunk_model=clone_model_for_chunk(model,strict=bool(strict_compatibility),debug=bool(debug),chunk_index=geometry.clip_index,context_frames=geometry.context_frames if geometry.continuation else None)
        sampled=sample_chunk(model=chunk_model,conditioning=conditioning,latent=latent,sampler=sampler,sigmas=sigmas,seed=seed,enable_preview=bool(enable_preview))
        if context_before is not None and video_context is not None: assert_context_unchanged(video_context,audio_context,context_before)
        entry=make_chunk_entry(latent=sampled,plan=chunk_plan,prompt=prompt,prompt_hash=prompt_hash_value,seed=seed,context_frames=geometry.context_frames,motion_score=geometry.motion_score,reused=False); previous_state=entry_to_state(entry); entries.append(entry)
        _record_context_diagnostics(tracker=context_diagnostics,reports=sampling_reports,state=previous_state,continuity=continuity,reused=False,continuation_method=continuation_method,audio_continuity=audio_continuity,driving_audio_active=driving_audio_source is not None)
        if storage_controller is not None: storage_controller.commit_chunk(entry, position=sequence_index)
        retained_frames+=geometry.net_frames
        presentation=descriptor.presentation_contract
        sampling_reports.append(
            f"physical chunk {sequence_index+1}: group={descriptor.group_id}, logical={list(descriptor.logical_indices)}, "
            f"R/C/F={descriptor.retained_before}/{descriptor.context_frames}/{descriptor.total_frames}, "
            f"global=[{descriptor.global_start_frame},{descriptor.global_end_frame}), compiler={compiled.compiler_version}, "
            f"descriptor={descriptor.digest[:16]}, conditioning={compiled.physical_conditioning_hash[:16]}, "
            f"text={compiled.text_sha256[:16]}, tokens={token_telemetry.get('token_count')}, "
            f"pictures={presentation.get('public_to_qwen_picture',{})}, first={presentation.get('include_first')}, last={presentation.get('include_last')}."
        )
        sampling_reports.append(f"chunk {sequence_index+1}/{chunks}: seed={seed}, frames={chunk_plan['total_frames']}, trim={chunk_plan['trim_frames']}, retained_total={retained_frames}, method={continuation_method}, context={geometry.context_frames} ({geometry.reason}), motion={geometry.motion_score:.6f}, "+(f"interop=emitted actual_prefix={CONTINUUM_ACTUAL_PREFIX_STEPS} consumer=not_observable" if context_before is not None else "interop=not_emitted"))
        del sampled,latent,conditioning,base_conditioning,chunk_model,chunk_cache
        if timeline_video_source is not None and timeline_video_assets is not None: del timeline_video_assets
    if terminal_merge_pending:
        pair_start=chunks-2
        if len(entries)!=pair_start:
            raise SequenceRuntimeError(f"terminal merged pair expected {pair_start} completed chunks, got {len(entries)}")
        if terminal_prompt is None:
            raise SequenceRuntimeError("terminal merged pair has no physical prompt")
        seed_nonce=int(reroll_nonce) if int(reroll_from_chunk)>0 else 0
        terminal_seed_plan=_terminal_physical_seed_plan(base_seed=base_seed,physical_window_start_index=pair_start,reroll_nonce=seed_nonce)
        physical_seed=int(terminal_seed_plan["physical_seed"]); initial_pair=previous_state is None
        contract=_terminal_pair_contract(initial_pair=initial_pair,chunk_seconds=chunk_seconds)
        physical_frames=int(contract["physical_frames"]); physical_context_frames=int(contract["physical_context_frames"])
        descriptor=_terminal_descriptor(
            pair_start=pair_start,chunks=chunks,retained_before=retained_frames,contract=contract,
            continuation_method=continuation_method,terminal_prompt_policy=terminal_prompt_policy,
            assets=assets,initial_pair=initial_pair,reference_assets=reference_assets,
            reference_audio_source=reference_audio_source,reference_video_source=reference_video_source,
            target_duration_frames=target_duration_frames,
        )
        base_conditioning,compiled,physical_meta,terminal_key=encode_physical_prompt_conditioning(
            clip=clip,plan=plan,descriptor=descriptor,legacy_text=terminal_prompt,assets=assets,
            include_first=initial_pair,include_last=True,reference_assets=reference_assets,
            reference_audio_assets=reference_audio_assets,timeline_video_assets=reference_video_assets,cache=cache,
        )
        token_telemetry=conditioning_telemetry(base_conditioning)
        latent=empty_h3_latent(width,height,physical_frames)
        base_conditioning=attach_keyframes(base_conditioning,frame_count=physical_frames,first_latent=assets.first_latent if initial_pair else None,last_latent=assets.last_latent)
        video_context=None; audio_context=None; context_before=None; motion_score=0.0
        if initial_pair:
            conditioning=base_conditioning; physical_clip_index=1
            reason="Core-equivalent initial 10-second FL2VA sample"
        else:
            selected_context,motion_score,selected_reason=choose_continuation_context_frames(method=continuation_method,continuity=continuity,state=previous_state,audio_continuity=bool(audio_continuity),driving_audio_active=driving_audio_source is not None)
            if selected_context!=TERMINAL_MERGE_CONTEXT_FRAMES or continuation_method!=CONTINUATION_GUIDE:
                raise SequenceRuntimeError("long terminal merge requires Guide / Motion Context with an exact 22-frame profile")
            carry_generated_audio=bool(audio_continuity) and driving_audio_source is None
            video_context,audio_context,grid_offset=select_context(previous_state,TERMINAL_MERGE_CONTEXT_FRAMES,include_audio=carry_generated_audio)
            context_before=context_fingerprint(video_context,audio_context)
            conditioning=prepare_conditioning(base_conditioning,video_context=video_context,audio_context=audio_context,audio_grid_offset=grid_offset,context_frames=TERMINAL_MERGE_CONTEXT_FRAMES,new_frame_count=physical_frames,first_frame_policy=POLICY_REPLACE,preserve_last_frame=True)
            physical_clip_index=int(previous_state["clip_index"])+1
            reason=f"10-second terminal sample with 22-frame Guide context ({selected_reason})"
        driving_audio_latent=slice_driving_audio_latent(driving_audio_assets,cumulative_retained_before=retained_frames,total_frames=physical_frames,trim_frames=physical_context_frames,fps=FPS)
        conditioning=attach_driving_audio(conditioning,driving_audio_latent)
        chunk_model=clone_model_for_chunk(model,strict=bool(strict_compatibility),debug=bool(debug),chunk_index=physical_clip_index,context_frames=physical_context_frames if not initial_pair else None)
        sampled=sample_chunk(model=chunk_model,conditioning=conditioning,latent=latent,sampler=sampler,sigmas=sigmas,seed=physical_seed,enable_preview=bool(enable_preview))
        if context_before is not None and video_context is not None: assert_context_unchanged(video_context,audio_context,context_before)
        sampled_video,sampled_audio=latent_to_cpu(sampled); logical_parts=_split_terminal_merged_latents(sampled_video,sampled_audio,contract)
        sampling_reports.append(f"terminal physical sample: pair={pair_start+1}-{chunks}, seed={physical_seed}, frames={physical_frames}, prompt_policy={terminal_prompt_policy}, trim={physical_context_frames}, method={continuation_method}, reason={reason}, compiler={compiled.compiler_version}, descriptor={descriptor.digest[:16]}, conditioning={compiled.physical_conditioning_hash[:16]}, tokens={token_telemetry.get('token_count')}")
        for role,(video_part,audio_part) in enumerate(logical_parts):
            sequence_index=pair_start+role; total_frames=int(contract["logical_frames"][role]); trim_frames=int(contract["logical_trims"][role]); continuation=bool(trim_frames); clip_index=physical_clip_index+role
            context_frames=trim_frames if continuation else 5
            chunk_plan=make_plan(continuation=continuation,clip_index=clip_index,total_frames=total_frames,trim_frames=trim_frames,width=width,height=height,context_frames=context_frames,state_capacity_frames=largest_context_capacity(total_frames-trim_frames),requested_extend_seconds=chunk_seconds,debug=debug)
            chunk_plan=_mark_terminal_plan(chunk_plan,contract=contract,role=role); chunk_plan["physical_prompt"]=copy.deepcopy(physical_meta)
            logical_latent=latent_from_cpu(video_part,audio_part); logical_seed=int(terminal_seed_plan["logical_entry_seeds"][role])
            entry=make_chunk_entry(latent=logical_latent,plan=chunk_plan,prompt=prompts[sequence_index],prompt_hash=prompt_hashes[sequence_index],seed=logical_seed,context_frames=trim_frames,motion_score=motion_score,reused=False)
            entries.append(entry); previous_state=entry_to_state(entry)
            _record_context_diagnostics(tracker=context_diagnostics,reports=sampling_reports,state=previous_state,continuity=continuity,reused=False,continuation_method=continuation_method,audio_continuity=audio_continuity,driving_audio_active=driving_audio_source is not None)
            if storage_controller is not None: storage_controller.commit_chunk(entry,position=sequence_index)
            retained_frames+=int(chunk_plan["net_frames"])
            sampling_reports.append(f"chunk {sequence_index+1}/{chunks}: seed={logical_seed}, frames={total_frames}, trim={trim_frames}, retained_total={retained_frames}, shared_physical_sample={TERMINAL_MERGE_STRATEGY}")
            del logical_latent
        del sampled,sampled_video,sampled_audio,logical_parts,latent,conditioning,chunk_model
    if len(entries)!=chunks: raise SequenceRuntimeError(f"internal sequence length mismatch: expected {chunks}, got {len(entries)}")
    physical_settings=_physical_settings(entries,plan)
    if latent_only:
        last_state=entry_to_state(entries[-1]); parent_id=session.get("session_id") if session is not None else None
        settings={"continuity":continuity,"continuation_method":continuation_method,"audio_continuity":bool(audio_continuity),"exact_total_duration":False,"prompt_mode":plan["mode"],"conditioning_mode":conditioning_mode,"base_seed":int(base_seed),"reroll_nonce":int(reroll_nonce),"diagnostics_mode":diagnostics_mode,"initial_state_source":initial_state is not None,"latent_first":True,"first_frame_hash":assets.first_frame_hash,"last_frame_hash":assets.last_frame_hash,"reference_contract":reference_assets.contract if reference_assets is not None else None,"physical_prompt_contract":physical_settings}
        if terminal_merge_enabled: settings["terminal_merge"]={"version":TERMINAL_MERGE_CONTRACT_VERSION,"strategy":TERMINAL_MERGE_STRATEGY,"prompt_policy":terminal_prompt_policy}
        if continuation_method==CONTINUATION_NATIVE_MASKED: settings["native_mask_contract_version"]=NATIVE_MASK_CONTRACT_VERSION
        if reference_audio_source is not None: settings["reference_audio_contract"]=reference_audio_source.contract
        if driving_audio_source is not None: settings["driving_audio_contract"]=driving_audio_source.contract
        if reference_video_source is not None: settings["reference_video_contract"]=reference_video_source.contract
        if timeline_video_source is not None: settings["timeline_video_contract"]=timeline_video_source.contract
        new_session=make_session(chunks=entries,width=width,height=height,chunk_seconds=chunk_seconds,identity_hash=sequence_identity_hash,model_fingerprint_value=current_model_fingerprint,parent_session_id=parent_id,reroll_from_chunk=int(reroll_from_chunk),settings=settings)
        report_lines=[f"H3 Continuum V3 {PACKAGE_VERSION}",f"Conditioning mode: {conditioning_mode_label(conditioning_mode)}.",f"Continuation: {continuation_method}.",prompt_plan_report(plan),"Decode: external ComfyUI Core VAE nodes; full raw AV chunks retained.",accelerators,"Execution: physical geometry resolved once before Qwen conditioning; single call-local MODEL clone per physical sample; no internal VAE decode.",*reuse_notes]
        if diagnostics_mode!=DIAGNOSTICS_OFF: report_lines.extend(sampling_reports)
        report_lines.extend([session_summary(new_session),f"Output: {len(entries)} raw AV latent chunk(s); connect Core VAE Decode nodes, then H3 Continuum Assemble V3."])
        return entries,last_state,new_session,"\n".join(report_lines)
    if seam_correction==SEAM_CORRECTION_OFF: images,audio,decode_reports=decode_sequence(entries=entries,video_vae=video_vae,audio_vae=audio_vae,diagnostics_full=diagnostics_mode==DIAGNOSTICS_FULL)
    else: images,audio,decode_reports=decode_sequence_with_seam(entries=entries,video_vae=video_vae,audio_vae=audio_vae,diagnostics_mode=diagnostics_mode,automatic=seam_correction==SEAM_CORRECTION_AUTO)
    duration_report=""
    if exact_total_duration:
        target_frames=int(round(chunks*chunk_seconds*FPS))
        if int(images.shape[0])<target_frames:
            raise SequenceRuntimeError(f"exact-duration sequence underflow: generated {int(images.shape[0])} frames for {target_frames}-frame target; refusing to repeat the final frame")
        images,audio,duration_report=enforce_total_frames(images,audio,target_frames=target_frames,preserve_final_frame=last_frame is not None)
    last_state=entry_to_state(entries[-1]); parent_id=session.get("session_id") if session is not None else None
    settings={"continuity":continuity,"continuation_method":continuation_method,"audio_continuity":bool(audio_continuity),"exact_total_duration":bool(exact_total_duration),"prompt_mode":plan["mode"],"base_seed":int(base_seed),"reroll_nonce":int(reroll_nonce),"diagnostics_mode":diagnostics_mode,"initial_state_source":initial_state is not None,"first_frame_hash":assets.first_frame_hash,"last_frame_hash":assets.last_frame_hash,"reference_contract":reference_assets.contract if reference_assets is not None else None,"physical_prompt_contract":physical_settings}
    if terminal_merge_enabled: settings["terminal_merge"]={"version":TERMINAL_MERGE_CONTRACT_VERSION,"strategy":TERMINAL_MERGE_STRATEGY,"prompt_policy":terminal_prompt_policy}
    if continuation_method==CONTINUATION_NATIVE_MASKED: settings["native_mask_contract_version"]=NATIVE_MASK_CONTRACT_VERSION
    new_session=make_session(chunks=entries,width=width,height=height,chunk_seconds=chunk_seconds,identity_hash=sequence_identity_hash,model_fingerprint_value=current_model_fingerprint,parent_session_id=parent_id,reroll_from_chunk=int(reroll_from_chunk),settings=settings)
    decoded_gib=float(images.shape[0])*float(width)*float(height)*3.0*4.0/(1024.0**3)
    report_lines=[f"H3 Continuum V2 {PACKAGE_VERSION}",prompt_plan_report(plan),f"Seam correction: {seam_correction}.",accelerators,"Execution: physical geometry resolved once before Qwen conditioning; single call-local MODEL clone per physical sample; decode deferred until sampling completed.",*reuse_notes]
    if diagnostics_mode!=DIAGNOSTICS_OFF: report_lines.extend([f"Decode RAM estimate: {decode_estimate_gib:.2f} GiB including transient headroom"+(f"; available at start {available_ram_gib:.2f} GiB." if available_ram_gib is not None else "."),*sampling_reports,*decode_reports])
    if duration_report: report_lines.append(duration_report)
    report_lines.extend([session_summary(new_session),f"Output: {images.shape[0]} frames ({images.shape[0]/FPS:.3f}s), audio samples={audio['waveform'].shape[-1]}, decoded tensor≈{decoded_gib:.2f} GiB."])
    return images,audio,last_state,new_session,"\n".join(report_lines)
# V3.0.1 hardening integration: Detailed Report only; generation semantics unchanged.
from ..hardening import run_sequence_with_hardening as _run_sequence_with_hardening

_run_sequence_v300 = run_sequence


def run_sequence(*args, **kwargs):
    return _run_sequence_with_hardening(_run_sequence_v300, args, kwargs)
