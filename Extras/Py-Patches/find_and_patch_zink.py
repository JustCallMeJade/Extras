#!/usr/bin/env python3
"""Walks a Mesa source tree, finds the files needed for xlib+zink support,
and patches them in place.

Scope: xlib+zink only. Virgl-specific changes (video-support removal in
virgl_context.c/virgl_screen.c/virgl's own meson.build) were dropped from
this script — they belonged to a separate, unrelated xlib+virgl series and
aren't needed for zink.

Verified against real uploaded file content (every file this script
touches):
    meson.build (root), meson.build (libgl-xlib), context.c,
    inline_sw_helper.h, zink_resource.c, zink_fence.c, zink_fence.h,
    zink_query.c, zink_batch.c, zink_batch.h, zink_screen.c, zink_screen.h,
    zink_types.h, zink_context.c

context.c's fix (relaxing the strict visual check for window-system
framebuffers via _mesa_is_winsys_fbo(), instead of deleting the check
outright) is kept because zink's xlib software path hits the same
MakeCurrent visual-compatibility issue as any other swrast-style winsys
backend — it isn't virgl-specific despite originating in that context.

zink_screen.c had significant version skew against the diff it was
originally written from. Corrections made against the real file:
  - zink_flush_frontbuffer()'s real body already uses
    zink_tc_context_unwrap(pctx) (single-arg), zink_resource_reference(),
    ctx->bs, ctx->swapchain, and zink_kopper_present_queue(screen, res,
    nboxes, sub_box) (4 args) — none of which matched the original diff's
    assumed body. The software-winsys branch and the zink_xlib_context
    "Context hack" are grafted onto this real body instead.
  - zink_internal_create_screen()'s timeline-required bailout actually
    checks two flags (have_KHR_timeline_semaphore AND
    feats12.timelineSemaphore) and is wrapped in a
    !screen->driver_name_is_inferred guard around the log call — adapted
    rather than blindly deleted.
  - zink_screen_init_semaphore()'s call site now checks its return value
    and falls back to have_KHR_timeline_semaphore = false on failure,
    instead of silently ignoring a real Vulkan API failure.
  - zink_create_screen(winsys, config) calls
    zink_internal_create_screen(config, -1, -1, 0) (4 args) and returns
    &ret->base, not ret — adapted signature/return accordingly.
  - This file never uses PIPE_TIMEOUT_INFINITE — used the real
    OS_TIMEOUT_INFINITE constant throughout instead.
  - noop_submit()'s queue-dispatch decision uses screen->threaded_submit,
    not screen->threaded — flush_queue is only initialized when
    threaded_submit is true (verified at its util_queue_init call site),
    so dispatching a job there when only `threaded` is true but
    `threaded_submit` is false would hit an uninitialized queue.
  - noop_submit() uses a real local `screen` var instead of relying on
    macro text-substitution through `n->screen`, and
    zink_screen_batch_id_wait() destroys its util_queue_fence on every
    path (it was leaked before).

zink_context.c also had version skew:
  - stall() uses ctx->last_batch_state (a zink_batch_state*), not
    ctx->last_fence (doesn't exist in this tree) — adapted accordingly.
  - zink_flush()'s tail uses a local `bs` (zink_batch_state*), not `fence`
    + zink_batch_state(fence) — adapted accordingly.
  - This tree uses OS_TIMEOUT_INFINITE throughout, not PIPE_TIMEOUT_INFINITE
    (which never appears anywhere in the real file) — used the real constant.
  - zink_context_create() has two return paths — an early return for
    non-threaded/compute-only contexts, and the normal threaded-context
    return further down. zink_xlib_context is assigned on both, so it's
    never left stale for contexts created through the early-return path.
  - zink_flush()'s added block gained a `!ctx->first_frame_done` guard.
    Without it, the explicit wait would run on every single end-of-frame
    flush forever, not just the first one — the field name and comment
    both say "first frame", so the guard makes the code do what it says.

zink_wait_on_batch() and zink_check_batch_completion()'s non-timeline
fallback logic is intentionally NOT included: it requires ctx->last_fence
and ctx->batch_mtx, neither of which exist anywhere in the uploaded
zink_types.h or zink_context.c, and writing it would mean inventing a
locking scheme with no basis in the real code. Both functions are left
doing their original unconditional zink_screen_timeline_wait() call.

get_batch_state() in the real zink_batch.c has a completely different body
(descriptor-buffer resize logic) than assumed — its fence-wait insertion is
NOT included for the same reason. post_submit()/submit_queue() likewise use
a (void *data, void *gdata, int thread_index) job-callback signature that
doesn't exist in the real zink_batch.c — not included; only the two
independent, verified insertions inside zink_batch_state_destroy() and
create_batch_state() are applied.
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
            "         struct zink_batch_state *bs = ctx->last_batch_state;\n"
            "         util_queue_fence_wait(&bs->flush_completed);\n"
            "      }\n"
            "   }\n"
            "   res->use_damage = false;\n"
            "\n"
            "   /* always verify that this was acquired */\n"
            "   assert(zink_kopper_acquired(res->obj->dt, res->obj->dt_idx));\n"
            "   zink_kopper_present_queue(screen, res, nboxes, sub_box);\n"
            "}",
            "   struct zink_screen *screen = zink_screen(pscreen);\n"
            "   struct zink_resource *res = zink_resource(pres);\n"
            "   struct zink_context *ctx = zink_context(pctx);\n"
            "\n"
            "   if (screen->winsys) {\n"
            "      /* software winsys path (e.g. xlib swrast): no native Vulkan\n"
            "       * swapchain exists to present through, so blit into the\n"
            "       * winsys displaytarget and let it composite/present instead. */\n"
            "      struct sw_winsys *winsys = screen->winsys;\n"
            "      void *map = winsys->displaytarget_map(winsys, res->dt, 0);\n"
            "\n"
            "      if (map) {\n"
            "         struct pipe_transfer *transfer = NULL;\n"
            "\n"
            "         // Context hack\n"
            "         pctx = zink_xlib_context;\n"
            "\n"
            "         void *res_map = pipe_texture_map(pctx, pres, level, layer, PIPE_MAP_READ, 0, 0,\n"
            "                                           u_minify(pres->width0, level),\n"
            "                                           u_minify(pres->height0, level),\n"
            "                                           &transfer);\n"
            "         if (res_map) {\n"
            "            util_copy_rect((ubyte*)map, pres->format, res->dt_stride, 0, 0,\n"
            "                           transfer->box.width, transfer->box.height,\n"
            "                           (const ubyte*)res_map, transfer->stride, 0, 0);\n"
            "            pipe_texture_unmap(pctx, transfer);\n"
            "         }\n"
            "         winsys->displaytarget_unmap(winsys, res->dt);\n"
            "      }\n"
            "\n"
            "      winsys->displaytarget_display(winsys, res->dt, winsys_drawable_handle, sub_box);\n"
            "      return;\n"
            "   }\n"
            "\n"
            "   /* native Vulkan swapchain path */\n"
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
            "         struct zink_batch_state *bs = ctx->last_batch_state;\n"
            "         util_queue_fence_wait(&bs->flush_completed);\n"
            "      }\n"
            "   }\n"
            "   res->use_damage = false;\n"
            "\n"
            "   /* always verify that this was acquired */\n"
            "   assert(zink_kopper_acquired(res->obj->dt, res->obj->dt_idx));\n"
            "   zink_kopper_present_queue(screen, res, nboxes, sub_box);\n"
            "}",
        ),
        (
            "   if (success)\n"
            "      zink_screen_update_last_finished(screen, batch_id);\n"
            "\n"
            "   return success;\n"
            "}\n"
            "\n"
            "static uint32_t\n"
            "zink_get_loader_version(struct zink_screen *screen)",
            "   if (success)\n"
            "      zink_screen_update_last_finished(screen, batch_id);\n"
            "\n"
            "   return success;\n"
            "}\n"
            "\n"
            "struct noop_submit_info {\n"
            "   struct zink_screen *screen;\n"
            "   VkFence fence;\n"
            "};\n"
            "\n"
            "static void\n"
            "noop_submit(void *data, void *gdata, int thread_index)\n"
            "{\n"
            "   struct noop_submit_info *n = data;\n"
            "   struct zink_screen *screen = n->screen;\n"
            "   VkSubmitInfo si = {0};\n"
            "   si.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;\n"
            "   simple_mtx_lock(screen->queue_lock);\n"
            "   if (VKSCR(QueueSubmit)(screen->threaded_submit ? screen->queue_sparse : screen->queue,\n"
            "                     1, &si, n->fence) != VK_SUCCESS) {\n"
            "      mesa_loge(\"ZINK: vkQueueSubmit failed\");\n"
            "      screen->device_lost = true;\n"
            "   }\n"
            "   simple_mtx_unlock(screen->queue_lock);\n"
            "}\n"
            "\n"
            "bool\n"
            "zink_screen_batch_id_wait(struct zink_screen *screen, uint32_t batch_id, uint64_t timeout)\n"
            "{\n"
            "   if (zink_screen_check_last_finished(screen, batch_id))\n"
            "      return true;\n"
            "\n"
            "   if (screen->info.have_KHR_timeline_semaphore)\n"
            "      return zink_screen_timeline_wait(screen, batch_id, timeout);\n"
            "\n"
            "   if (!timeout)\n"
            "      return false;\n"
            "\n"
            "   uint32_t new_id = 0;\n"
            "   while (!new_id)\n"
            "      new_id = p_atomic_inc_return(&screen->curr_batch);\n"
            "   VkResult ret;\n"
            "   struct noop_submit_info n;\n"
            "   int64_t abs_timeout = os_time_get_absolute_timeout(timeout);\n"
            "   uint64_t remaining = OS_TIMEOUT_INFINITE;\n"
            "   VkFenceCreateInfo fci = {0};\n"
            "   struct util_queue_fence fence;\n"
            "   util_queue_fence_init(&fence);\n"
            "   fci.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;\n"
            "\n"
            "   if (VKSCR(CreateFence)(screen->dev, &fci, NULL, &n.fence) != VK_SUCCESS) {\n"
            "      mesa_loge(\"ZINK: vkCreateFence failed\");\n"
            "      util_queue_fence_destroy(&fence);\n"
            "      return false;\n"
            "   }\n"
            "\n"
            "   n.screen = screen;\n"
            "   if (screen->threaded_submit) {\n"
            "      /* must use thread dispatch for sanity */\n"
            "      util_queue_add_job(&screen->flush_queue, &n, &fence, noop_submit, NULL, 0);\n"
            "      util_queue_fence_wait(&fence);\n"
            "   } else {\n"
            "      noop_submit(&n, NULL, 0);\n"
            "   }\n"
            "   util_queue_fence_destroy(&fence);\n"
            "   if (timeout != OS_TIMEOUT_INFINITE) {\n"
            "      int64_t time_ns = os_time_get_nano();\n"
            "      remaining = abs_timeout > time_ns ? abs_timeout - time_ns : 0;\n"
            "   }\n"
            "\n"
            "   if (remaining)\n"
            "      ret = VKSCR(WaitForFences)(screen->dev, 1, &n.fence, VK_TRUE, remaining);\n"
            "   else\n"
            "      ret = VKSCR(GetFenceStatus)(screen->dev, n.fence);\n"
            "   VKSCR(DestroyFence)(screen->dev, n.fence, NULL);\n"
            "   bool success = zink_screen_handle_vkresult(screen, ret);\n"
            "\n"
            "   if (success)\n"
            "      zink_screen_update_last_finished(screen, new_id);\n"
            "\n"
            "   return success;\n"
            "}\n"
            "\n"
            "static uint32_t\n"
            "zink_get_loader_version(struct zink_screen *screen)",
        ),
        (
            "   zink_internal_setup_moltenvk(screen);\n"
            "   if (!screen->info.have_KHR_timeline_semaphore && !screen->info.feats12.timelineSemaphore) {\n"
            "      if (!screen->driver_name_is_inferred)\n"
            "         mesa_loge(\"zink: KHR_timeline_semaphore is required\");\n"
            "      goto fail;\n"
            "   }",
            "   zink_internal_setup_moltenvk(screen);\n"
            "   if (!screen->info.have_KHR_timeline_semaphore && !screen->info.feats12.timelineSemaphore) {\n"
            "      if (!screen->driver_name_is_inferred)\n"
            "         mesa_loge(\"zink: timeline semaphores not supported, using fence fallback\");\n"
            "   }",
        ),
        (
            "   if (!zink_screen_init_semaphore(screen)) {\n"
            "      if (!screen->driver_name_is_inferred)\n"
            "         mesa_loge(\"zink: failed to create timeline semaphore\");\n"
            "      goto fail;\n"
            "   }",
            "   if (debug_get_bool_option(\"ZINK_NO_TIMELINES\", false))\n"
            "      screen->info.have_KHR_timeline_semaphore = false;\n"
            "   if (screen->info.have_KHR_timeline_semaphore && !zink_screen_init_semaphore(screen)) {\n"
            "      if (!screen->driver_name_is_inferred)\n"
            "         mesa_loge(\"zink: failed to create timeline semaphore, using fence fallback\");\n"
            "      screen->info.have_KHR_timeline_semaphore = false;\n"
            "   }",
        ),
        (
            "   struct zink_screen *ret = zink_internal_create_screen(config, -1, -1, 0);\n"
            "   if (ret) {\n"
            "      ret->drm_fd = -1;\n"
            "   }\n"
            "\n"
            "   return &ret->base;",
            "   struct zink_screen *ret = zink_internal_create_screen(config, -1, -1, 0);\n"
            "   if (ret) {\n"
            "      ret->winsys = winsys;\n"
            "      ret->drm_fd = -1;\n"
            "   }\n"
            "\n"
            "   return &ret->base;",
        ),
    ],
    "zink_types.h": [
        (
            "   bool swapchain;\n"
            "   bool dmabuf;\n"
            "   bool unflushed_transient; //format view transient has newer data than parent\n"
            "   bool subdata; //doing subdata call\n"
            "   unsigned dt_stride;",
            "   bool swapchain;\n"
            "   bool dmabuf;\n"
            "   bool unflushed_transient; //format view transient has newer data than parent\n"
            "   bool subdata; //doing subdata call\n"
            "\n"
            "   struct sw_displaytarget *dt;\n"
            "\n"
            "   unsigned dt_stride;",
        ),
        (
            "   simple_mtx_t dt_lock;\n"
            "\n"
            "   bool device_lost;\n"
            "   int drm_fd;",
            "   simple_mtx_t dt_lock;\n"
            "\n"
            "   bool device_lost;\n"
            "\n"
            "   struct sw_winsys *winsys;\n"
            "\n"
            "   int drm_fd;",
        ),
        (
            "   uint64_t curr_batch; //the current batch id\n"
            "   uint32_t last_finished;\n"
            "   VkSemaphore sem;\n"
            "   VkFence fence;",
            "   uint64_t curr_batch; //the current batch id\n"
            "   uint32_t last_finished;\n"
            "   VkSemaphore sem;\n"
            "   VkSemaphore prev_sem;\n"
            "   VkFence fence;",
        ),
        (
            "struct zink_fence {\n"
            "   uint64_t batch_id;\n"
            "   bool submitted;\n"
            "   bool completed;\n"
            "   struct util_dynarray mfences;\n"
            "};",
            "struct zink_fence {\n"
            "   uint64_t batch_id;\n"
            "   bool submitted;\n"
            "   bool completed;\n"
            "   VkFence fence;\n"
            "   struct util_dynarray mfences;\n"
            "};",
        ),
        (
            "struct zink_batch_state {\n"
            "   struct zink_fence fence;\n"
            "   struct zink_batch_state *next;",
            "struct zink_batch_state {\n"
            "   struct zink_fence fence;\n"
            "   bool have_timelines;\n"
            "   struct zink_batch_state *next;",
        ),
        (
            "   bool oom_flush;\n"
            "   bool oom_stall;\n"
            "   bool track_renderpasses;\n"
            "   bool no_reorder;\n"
            "   struct zink_batch_state *bs;",
            "   bool oom_flush;\n"
            "   bool oom_stall;\n"
            "   bool track_renderpasses;\n"
            "   bool no_reorder;\n"
            "   bool have_timelines;\n"
            "   bool first_frame_done;\n"
            "   struct zink_batch_state *bs;",
        ),
    ],
    "zink_context.c": [
        (
            '#define XXH_INLINE_ALL\n'
            '#include "util/xxhash.h"\n',
            '#define XXH_INLINE_ALL\n'
            '#include "util/xxhash.h"\n'
            '\n'
            'struct pipe_context* zink_xlib_context;\n',
        ),
        (
            "   if (!(flags & PIPE_CONTEXT_PREFER_THREADED) || flags & PIPE_CONTEXT_COMPUTE_ONLY) {\n"
            "      return &ctx->base;\n"
            "   }",
            "   if (!(flags & PIPE_CONTEXT_PREFER_THREADED) || flags & PIPE_CONTEXT_COMPUTE_ONLY) {\n"
            "      zink_xlib_context = &ctx->base;\n"
            "      return &ctx->base;\n"
            "   }",
        ),
        (
            "      ctx->base.set_context_param = zink_set_context_param;\n"
            "   }\n"
            "\n"
            "   return (struct pipe_context*)tc;\n"
            "\n"
            "fail:",
            "      ctx->base.set_context_param = zink_set_context_param;\n"
            "   }\n"
            "\n"
            "   zink_xlib_context = (struct pipe_context*)tc;\n"
            "\n"
            "   return (struct pipe_context*)tc;\n"
            "\n"
            "fail:",
        ),
        (
            "static void\n"
            "stall(struct zink_context *ctx)\n"
            "{\n"
            "   struct zink_screen *screen = zink_screen(ctx->base.screen);\n"
            "   sync_flush(ctx, ctx->last_batch_state);\n"
            "   zink_screen_timeline_wait(screen, ctx->last_batch_state->fence.batch_id, OS_TIMEOUT_INFINITE);\n"
            "}",
            "static void\n"
            "stall(struct zink_context *ctx)\n"
            "{\n"
            "   struct zink_screen *screen = zink_screen(ctx->base.screen);\n"
            "   sync_flush(ctx, ctx->last_batch_state);\n"
            "   if (ctx->have_timelines)\n"
            "      zink_screen_timeline_wait(screen, ctx->last_batch_state->fence.batch_id, OS_TIMEOUT_INFINITE);\n"
            "   else\n"
            "      zink_vkfence_wait(screen, &ctx->last_batch_state->fence, OS_TIMEOUT_INFINITE);\n"
            "}",
        ),
        (
            "   if (bs) {\n"
            "      if (!(flags & (PIPE_FLUSH_DEFERRED | PIPE_FLUSH_ASYNC)))\n"
            "         sync_flush(ctx, bs);\n"
            "   }\n"
            "}\n"
            "\n"
            "void",
            "   if (bs) {\n"
            "      if (!(flags & (PIPE_FLUSH_DEFERRED | PIPE_FLUSH_ASYNC)))\n"
            "         sync_flush(ctx, bs);\n"
            "\n"
            "      if (flags & PIPE_FLUSH_END_OF_FRAME && !(flags & TC_FLUSH_ASYNC) && !deferred && !ctx->first_frame_done) {\n"
            "         /* if the first frame has not yet occurred, we need an explicit fence here\n"
            "         * in some cases in order to correctly draw the first frame, though it's\n"
            "         * unknown at this time why this is the case\n"
            "         */\n"
            "         if (ctx->have_timelines)\n"
            "            zink_screen_timeline_wait(screen, bs->fence.batch_id, OS_TIMEOUT_INFINITE);\n"
            "         else\n"
            "            zink_vkfence_wait(screen, &bs->fence, OS_TIMEOUT_INFINITE);\n"
            "\n"
            "         ctx->first_frame_done = true;\n"
            "      }\n"
            "   }\n"
            "}\n"
            "\n"
            "void",
        ),
    ],
}

# meson.build appears in nearly every directory of a Meson project, so the
# two non-root meson.build entries above need path hints to disambiguate them.
PATH_HINTS = {
    "meson.build": None,  # root only
    "meson.build:libgl-xlib": os.path.join("gallium", "targets", "libgl-xlib"),
}


def find_files(root):
    """Return {fix_key: [full paths found]}."""
    found = {key: [] for key in FIXES}

    root_meson = os.path.join(root, "meson.build")
    if os.path.isfile(root_meson):
        found["meson.build"].append(root_meson)

    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename == "meson.build":
                hint = PATH_HINTS["meson.build:libgl-xlib"]
                if dirpath.replace("\\", "/").endswith(hint.replace("\\", "/")):
                    found["meson.build:libgl-xlib"].append(os.path.join(dirpath, filename))
            elif filename in FIXES:
                found[filename].append(os.path.join(dirpath, filename))

    return found


def apply_fixes(path, replacements):
    with open(path) as f:
        content = f.read()

    changed = False
    for old, new in replacements:
        if new in content:
            print(f"  {path}: already applied, one fix skipped")
        elif old in content:
            content = content.replace(old, new, 1)
            changed = True
        else:
            print(f"  {path}: anchor not found, one fix skipped")

    if changed:
        with open(path, "w") as f:
            f.write(content)
        print(f"  {path}: patched")
    else:
        print(f"  {path}: no changes applied")


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    found = find_files(root)

    for key, replacements in FIXES.items():
        paths = found[key]
        if not paths:
            print(f"{key}: not found under {root}")
            continue
        for path in paths:
            apply_fixes(path, replacements)


if __name__ == "__main__":
    main()
