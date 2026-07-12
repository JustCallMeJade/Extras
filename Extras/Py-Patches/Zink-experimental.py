#!/usr/bin/env python3
"""Walks a Mesa source tree, finds the files needed for xlib+zink support,
and patches them in place.

Verified against real uploaded file content (all files in this script):
    meson.build (root), meson.build (libgl-xlib), meson.build (virgl),
    inline_sw_helper.h, virgl_context.c, virgl_screen.c, context.c,
    zink_query.c, zink_batch.c, zink_types.h, zink_fence.c, zink_fence.h,
    zink_batch.h, zink_screen.h, zink_context.c, zink_screen.c,
    zink_resource.c

zink_screen.c had the same kind of version skew as zink_context.c/
zink_batch.c. Corrections made against the real file:
  - zink_flush_frontbuffer()'s real body already uses
    zink_tc_context_unwrap(pctx) (single-arg), zink_resource_reference(),
    ctx->bs, ctx->swapchain, and zink_kopper_present_queue(screen, res,
    nboxes, sub_box) (4 args) — none of which match the diff's assumed
    body. The software-winsys branch (and the zink_xlib_context "Context
    hack" from an earlier message) is grafted onto this real body instead.
  - zink_internal_create_screen()'s timeline-required bailout actually
    checks two flags (have_KHR_timeline_semaphore AND
    feats12.timelineSemaphore) and is wrapped in a
    !screen->driver_name_is_inferred guard around the log call — adapted
    rather than blindly deleted.
  - zink_screen_init_semaphore()'s call site now checks its return value
    and falls back to have_KHR_timeline_semaphore = false on failure,
    instead of silently ignoring a real Vulkan API failure as the pasted
    diff did.
  - zink_create_screen(winsys, config) calls
    zink_internal_create_screen(config, -1, -1, 0) (4 args) and returns
    &ret->base, not ret — adapted signature/return accordingly.
  - This file never uses PIPE_TIMEOUT_INFINITE (same as zink_context.c) —
    used OS_TIMEOUT_INFINITE throughout instead.
  - noop_submit()'s queue-dispatch decision uses screen->threaded_submit,
    not screen->threaded as the diff had — flush_queue is only
    initialized when threaded_submit is true (verified at its
    util_queue_init call site), so dispatching a job there when only
    `threaded` is true but `threaded_submit` is false would hit an
    uninitialized queue.
  - noop_submit() still has the VKSCR-via-macro fix (uses a real local
    `screen` var) and zink_screen_batch_id_wait() still has the
    util_queue_fence_destroy() fix (was leaked on every call) from the
    earlier, pre-upload version of this fix.

IMPORTANT — version skew also found in zink_context.c and zink_batch.c:
The pasted diff was written against a more advanced zink revision than the
uploaded tree. Confirmed real differences:
  - stall() uses ctx->last_batch_state (a zink_batch_state*), not
    ctx->last_fence (doesn't exist in this tree) — adapted accordingly.
  - zink_flush()'s tail uses a local `bs` (zink_batch_state*), not `fence`
    + zink_batch_state(fence) — adapted accordingly.
  - This tree uses OS_TIMEOUT_INFINITE throughout, not PIPE_TIMEOUT_INFINITE
    (which never appears anywhere in the real file) — used the real constant.
  - zink_wait_on_batch() and zink_check_batch_completion() in the diff
    require ctx->last_fence and ctx->batch_mtx, NEITHER of which exist
    anywhere in the uploaded zink_types.h or zink_context.c. These two
    functions' non-timeline fallback logic is NOT included below — writing
    it would mean inventing a locking scheme with no basis in the real
    code. Both functions are left doing their original unconditional
    zink_screen_timeline_wait() call, same as before this series.
  - get_batch_state() in the real zink_batch.c has a completely different
    body (descriptor-buffer resize logic) than the diff assumes (a bare
    zink_reset_batch_state() call) — its fence-wait insertion is NOT
    included below for the same reason.
  - post_submit()/submit_queue() in the diff use a
    (void *data, void *gdata, int thread_index) job-callback signature that
    doesn't exist in the real zink_batch.c (real post_submit takes
    (struct zink_batch_state *bs, struct zink_screen *screen) directly) —
    these are NOT included below; only the two independent, verified
    insertions inside zink_batch_state_destroy() and create_batch_state()
    are applied.
  - zink_flush()'s added block also gained a `!ctx->first_frame_done`
    guard that the pasted diff didn't have. The diff set
    ctx->first_frame_done = true but never checked it anywhere, so as
    written the explicit wait would run on every single end-of-frame flush
    forever, not just the first one — the field name and comment both
    say "first frame", so this looks like a bug in the original diff, and
    the guard was added to make the code do what it says.
"""

import os
import sys

# Files whose anchors are confirmed against real uploaded source.
FIXES = {
    "meson.build": [
        (
            "    elif not with_gallium_swrast\n"
            "      error('xlib based GLX requires softpipe or llvmpipe.')",
            "    elif not with_gallium_swrast and not with_gallium_zink\n"
            "      error('xlib based GLX requires softpipe, llvmpipe, or zink.')",
        ),
    ],
    "context.c": [
        (
            "      if (!check_compatible(newCtx, drawBuffer)) {\n"
            "         _mesa_warning(newCtx,\n"
            '              "MakeCurrent: incompatible visuals for context and drawbuffer");\n'
            "         return GL_FALSE;\n"
            "      }",
            "      if (!check_compatible(newCtx, drawBuffer)) {\n"
            "         _mesa_warning(newCtx,\n"
            '              "MakeCurrent: incompatible visuals for context and drawbuffer");\n'
            "         if (!_mesa_is_winsys_fbo(drawBuffer))\n"
            "            return GL_FALSE;\n"
            "      }",
        ),
        (
            "      if (!check_compatible(newCtx, readBuffer)) {\n"
            "         _mesa_warning(newCtx,\n"
            '              "MakeCurrent: incompatible visuals for context and readbuffer");\n'
            "         return GL_FALSE;\n"
            "      }",
            "      if (!check_compatible(newCtx, readBuffer)) {\n"
            "         _mesa_warning(newCtx,\n"
            '              "MakeCurrent: incompatible visuals for context and readbuffer");\n'
            "         if (!_mesa_is_winsys_fbo(readBuffer))\n"
            "            return GL_FALSE;\n"
            "      }",
        ),
    ],
    "inline_sw_helper.h": [
        (
            '#ifdef GALLIUM_D3D12\n'
            '#include "d3d12/d3d12_public.h"\n'
            '#endif',
            '#ifdef GALLIUM_D3D12\n'
            '#include "d3d12/d3d12_public.h"\n'
            '#endif\n'
            '\n'
            '#ifdef GALLIUM_ZINK\n'
            '#include "zink/zink_public.h"\n'
            '#endif',
        ),
    ],
    "virgl_context.c": [
        (
            "   vctx->base.memory_barrier = virgl_memory_barrier;\n"
            "   vctx->base.emit_string_marker = virgl_emit_string_marker;\n"
            "\n"
            "   vctx->base.create_video_codec = virgl_video_create_codec;\n"
            "   vctx->base.create_video_buffer = virgl_video_create_buffer;\n"
            "\n"
            "   if (rs->caps.caps.v2.host_feature_check_version >= 7)",
            "   vctx->base.memory_barrier = virgl_memory_barrier;\n"
            "   vctx->base.emit_string_marker = virgl_emit_string_marker;\n"
            "\n"
            "   if (rs->caps.caps.v2.host_feature_check_version >= 7)",
        ),
    ],
    "virgl_screen.c": [
        (
            "static bool virgl_is_video_format_supported(struct pipe_screen *screen,\n"
            "                                            enum pipe_format format,\n"
            "                                            enum pipe_video_profile profile,\n"
            "                                            enum pipe_video_entrypoint entrypoint)\n"
            "{\n"
            "    return vl_video_buffer_is_format_supported(screen, format, profile, entrypoint);\n"
            "}",
            "static bool virgl_is_video_format_supported(struct pipe_screen *screen,\n"
            "                                            enum pipe_format format,\n"
            "                                            enum pipe_video_profile profile,\n"
            "                                            enum pipe_video_entrypoint entrypoint)\n"
            "{\n"
            "    return false;\n"
            "}",
        ),
    ],
    # --- UNVERIFIED below: anchors taken from the pasted diff's context,
    # not from an uploaded file. Will be skipped harmlessly if the anchor
    # doesn't match your actual tree. Upload these files to verify them.
    "meson.build:virgl": [
        (
            "  'virgl_transfer_queue.c',\n"
            "  'virgl_texture.c',\n"
            "  'virgl_tgsi.c',\n"
            "  'virgl_video.c',\n"
            ")",
            "  'virgl_transfer_queue.c',\n"
            "  'virgl_texture.c',\n"
            "  'virgl_tgsi.c',\n"
            ")",
        ),
    ],
    "meson.build:libgl-xlib": [
        (
            "dependencies : [dep_x11, idep_mesautil, dep_thread, dep_clock, dep_unwind, "
            "driver_swrast, driver_virgl, driver_asahi],",
            "dependencies : [dep_x11, idep_mesautil, dep_thread, dep_clock, dep_unwind, "
            "driver_swrast, driver_virgl, driver_asahi, driver_zink],",
        ),
    ],
    "zink_resource.c": [
        (
            '#include "zink_kopper.h"\n'
            '\n'
            '#ifdef VK_USE_PLATFORM_METAL_EXT',
            '#include "zink_kopper.h"\n'
            '\n'
            '#include "frontend/sw_winsys.h"\n'
            '\n'
            '#ifdef VK_USE_PLATFORM_METAL_EXT',
        ),
        (
            "      res->aspect = aspect_from_format(templ->format);\n"
            "   }\n"
            "\n"
            "   if (loader_private) {",
            "      res->aspect = aspect_from_format(templ->format);\n"
            "   }\n"
            "\n"
            "   if (screen->winsys && (templ->bind & PIPE_BIND_DISPLAY_TARGET)) {\n"
            "      struct sw_winsys *winsys = screen->winsys;\n"
            "      res->dt = winsys->displaytarget_create(screen->winsys,\n"
            "                                             res->base.b.bind,\n"
            "                                             res->base.b.format,\n"
            "                                             templ->width0,\n"
            "                                             templ->height0,\n"
            "                                             64, NULL,\n"
            "                                             &res->dt_stride);\n"
            "   }\n"
            "\n"
            "   if (loader_private) {",
        ),
    ],
    "zink_fence.c": [
        (
            "   bool success = zink_screen_timeline_wait(screen, fence->batch_id, timeout_ns);\n"
            "\n"
            "   if (success) {\n"
            "      fence->completed = true;\n"
            "      bs->usage.usage = 0;\n"
            "      zink_screen_update_last_finished(screen, fence->batch_id);\n"
            "   }\n"
            "   return success;\n"
            "}\n"
            "\n"
            "static bool\n"
            "zink_fence_finish(struct zink_screen *screen, struct pipe_context *pctx, struct zink_tc_fence *mfence,",
            "   bool success = zink_screen_timeline_wait(screen, fence->batch_id, timeout_ns);\n"
            "\n"
            "   if (success) {\n"
            "      fence->completed = true;\n"
            "      bs->usage.usage = 0;\n"
            "      zink_screen_update_last_finished(screen, fence->batch_id);\n"
            "   }\n"
            "   return success;\n"
            "}\n"
            "\n"
            "bool\n"
            "zink_vkfence_wait(struct zink_screen *screen, struct zink_fence *fence, uint64_t timeout_ns)\n"
            "{\n"
            "   if (screen->device_lost)\n"
            "      return true;\n"
            "   if (fence->completed)\n"
            "      return true;\n"
            "\n"
            "   assert(fence->batch_id);\n"
            "   assert(fence->submitted);\n"
            "\n"
            "   VkResult ret;\n"
            "   if (timeout_ns)\n"
            "      ret = VKSCR(WaitForFences)(screen->dev, 1, &fence->fence, VK_TRUE, timeout_ns);\n"
            "   else\n"
            "      ret = VKSCR(GetFenceStatus)(screen->dev, fence->fence);\n"
            "   bool success = zink_screen_handle_vkresult(screen, ret);\n"
            "\n"
            "   if (success) {\n"
            "      fence->completed = true;\n"
            "      zink_batch_state(fence)->usage.usage = 0;\n"
            "      zink_screen_update_last_finished(screen, fence->batch_id);\n"
            "   }\n"
            "   return success;\n"
            "}\n"
            "\n"
            "static bool\n"
            "zink_fence_finish(struct zink_screen *screen, struct pipe_context *pctx, struct zink_tc_fence *mfence,",
        ),
        (
            "   if ((fence->submitted && zink_screen_check_last_finished(screen, fence->batch_id)) ||\n"
            "       (!fence->submitted && submit_diff))\n"
            "      return true;\n"
            "\n"
            "   return fence_wait(screen, fence, timeout_ns);\n"
            "}",
            "   if ((fence->submitted && zink_screen_check_last_finished(screen, fence->batch_id)) ||\n"
            "       (!fence->submitted && submit_diff))\n"
            "      return true;\n"
            "\n"
            "   if (screen->info.have_KHR_timeline_semaphore)\n"
            "      return fence_wait(screen, fence, timeout_ns);\n"
            "\n"
            "   return zink_vkfence_wait(screen, fence, timeout_ns);\n"
            "}",
        ),
    ],
    "zink_fence.h": [
        (
            "void\n"
            "zink_screen_fence_init(struct pipe_screen *pscreen);\n"
            "\n"
            "void\n"
            "zink_fence_clear_resources(struct zink_screen *screen, struct zink_fence *fence);",
            "void\n"
            "zink_screen_fence_init(struct pipe_screen *pscreen);\n"
            "\n"
            "bool\n"
            "zink_vkfence_wait(struct zink_screen *screen, struct zink_fence *fence, uint64_t timeout_ns);\n"
            "\n"
            "void\n"
            "zink_fence_clear_resources(struct zink_screen *screen, struct zink_fence *fence);",
        ),
    ],
    "zink_query.c": [
        (
            "   if (zink_batch_usage_is_unflushed(query->batch_uses)) {\n"
            "      if (!threaded_query(q)->flushed)\n"
            "         pctx->flush(pctx, NULL, 0);\n"
            "      if (!wait)\n"
            "         return false;\n"
            "   }\n",
            "   if (zink_batch_usage_is_unflushed(query->batch_uses)) {\n"
            "      if (!threaded_query(q)->flushed)\n"
            "         pctx->flush(pctx, NULL, 0);\n"
            "      if (!wait)\n"
            "         return false;\n"
            "   }\n"
            "   else if (!threaded_query(q)->flushed &&\n"
            "              /* timeline drivers can wait during buffer map */\n"
            "              !zink_screen(pctx->screen)->info.have_KHR_timeline_semaphore)\n"
            "      zink_batch_usage_check_completion(ctx, query->batch_uses);\n",
        ),
    ],
    "zink_batch.c": [
        (
            "   cnd_destroy(&bs->usage.flush);\n"
            "   mtx_destroy(&bs->usage.mtx);\n"
            "\n"
            "   if (bs->cmdbuf)",
            "   cnd_destroy(&bs->usage.flush);\n"
            "   mtx_destroy(&bs->usage.mtx);\n"
            "\n"
            "   if (bs->fence.fence)\n"
            "      VKSCR(DestroyFence)(screen->dev, bs->fence.fence, NULL);\n"
            "\n"
            "   if (bs->cmdbuf)",
        ),
        (
            "   struct zink_batch_state *bs = rzalloc(NULL, struct zink_batch_state);\n"
            "   VkCommandPoolCreateInfo cpci = {0};",
            "   struct zink_batch_state *bs = rzalloc(NULL, struct zink_batch_state);\n"
            "\n"
            "   bs->have_timelines = ctx->have_timelines;\n"
            "\n"
            "   VkCommandPoolCreateInfo cpci = {0};",
        ),
        (
            "   if (!zink_batch_descriptor_init(screen, bs))\n"
            "      goto fail;\n"
            "\n"
            "   util_queue_fence_init(&bs->flush_completed);",
            "   if (!zink_batch_descriptor_init(screen, bs))\n"
            "      goto fail;\n"
            "\n"
            "   if (!bs->have_timelines) {\n"
            "      VkFenceCreateInfo fci = {0};\n"
            "      fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;\n"
            "\n"
            "      if (VKSCR(CreateFence)(screen->dev, &fci, NULL, &bs->fence.fence) != VK_SUCCESS)\n"
            "         goto fail;\n"
            "   }\n"
            "\n"
            "   util_queue_fence_init(&bs->flush_completed);",
        ),
        (
            "   if (zink_batch_usage_is_unflushed(u))\n"
            "      return false;\n"
            "\n"
            "   return zink_screen_timeline_wait(screen, u->usage, 0);\n"
            "}",
            "   if (zink_batch_usage_is_unflushed(u))\n"
            "      return false;\n"
            "\n"
            "   if (screen->info.have_KHR_timeline_semaphore)\n"
            "      return zink_screen_timeline_wait(screen, u->usage, 0);\n"
            "\n"
            "   return zink_screen_batch_id_wait(screen, u->usage, 0);\n"
            "}",
        ),
    ],
    "zink_screen.c": [
        (
            '#include "util/u_cpu_detect.h"\n',
            '#include "util/u_cpu_detect.h"\n'
            '\n'
            '#include "frontend/sw_winsys.h"\n'
            '\n'
            'extern struct pipe_context* zink_xlib_context;\n',
        ),
        (
            "   if (screen->sem)\n"
            "      VKSCR(DestroySemaphore)(screen->dev, screen->sem, NULL);\n"
            "\n"
            "   if (screen->fence)",
            "   if (screen->sem)\n"
            "      VKSCR(DestroySemaphore)(screen->dev, screen->sem, NULL);\n"
            "\n"
            "   if (screen->prev_sem)\n"
            "      VKSCR(DestroySemaphore)(screen->dev, screen->prev_sem, NULL);\n"
            "\n"
            "   if (screen->fence)",
        ),
        (
            "   struct zink_screen *screen = zink_screen(pscreen);\n"
            "   struct zink_resource *res = zink_resource(pres);\n"
            "   struct zink_context *ctx = zink_context(pctx);\n"
            "\n"
            "   /* if the surface is no longer a swapchain, this is a no-op */\n"
            "   if (!zink_is_swapchain(res))\n"
            "      return;\n"
            "\n"
            "   ctx = zink_tc_context_unwrap(pctx);\n"
            "\n"
            "   if (!zink_kopper_acquired(res->obj->dt, res->obj->dt_idx)) {\n"
            "      /* swapbuffers to an undefined surface: acquire and present garbage */\n"
            "      zink_kopper_acquire(ctx, res, UINT64_MAX);\n"
            "      zink_resource_reference(&ctx->needs_present, res);\n"
            "      /* set batch usage to submit acquire semaphore */\n"
            "      zink_batch_resource_usage_set(ctx->bs, res, true, false);\n"
            "      /* ensure the resource is set up to present garbage */\n"
            "      ctx->base.flush_resource(&ctx->base, pres);\n"
            "   }\n"
            "\n"
            "   /* handle any outstanding acquire submits (not just from above) */\n"
            "   if (ctx->swapchain || ctx->needs_present) {\n"
            "      ctx->bs->has_work = true;\n"
            "      pctx->flush(pctx, NULL, PIPE_FLUSH_END_OF_FRAME);\n"
            "      if (ctx->last_batch_state && screen->threaded_submit) {\n"
            "         struct zink_batch_state *bs = ctx->last_batch_state
