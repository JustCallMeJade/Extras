#!/usr/bin/env python3
"""Walks a Mesa source tree, finds the files needed for xlib+zink software
display support, and patches them in place.

Scope: xlib display support ONLY. The original series this was extracted
from bundled in an unrelated feature — a timeline-semaphore fallback for
Vulkan devices lacking VK_KHR_timeline_semaphore (zink_fence.c's
zink_vkfence_wait, zink_batch.c's have_timelines/noop_submit/
zink_screen_batch_id_wait, zink_query.c's completion-check branch,
zink_screen.c's zink_internal_create_screen relaxation, and the
have_timelines/first_frame_done fields + stall()/zink_flush() branching in
zink_context.c). None of that is needed to get a window on screen via
xlib+zink; it's a separate concern about running zink on GPUs without
timeline semaphores, orthogonal to xlib support. All of it has been
removed from this script. zink_fence.c, zink_fence.h, zink_query.c, and
zink_batch.c are no longer touched at all.

Verified against real uploaded file content (every file this script
touches):
    meson.build (root), meson.build (libgl-xlib), context.c,
    inline_sw_helper.h, zink_resource.c, zink_screen.c, zink_types.h,
    zink_context.c

context.c's fix (relaxing the strict visual check for window-system
framebuffers via _mesa_is_winsys_fbo(), instead of deleting the check
outright) is kept because zink's xlib software path hits the same
MakeCurrent visual-compatibility issue as any other swrast-style winsys
backend.

zink_screen.c's zink_flush_frontbuffer() had significant version skew
against the diff this was originally extracted from. The real body already
uses zink_tc_context_unwrap(pctx) (single-arg), zink_resource_reference(),
ctx->bs, ctx->swapchain, and zink_kopper_present_queue(screen, res, nboxes,
sub_box) (4 args) — the software-winsys branch and the zink_xlib_context
"Context hack" are grafted onto this real body. zink_create_screen(winsys,
config) calls zink_internal_create_screen(config, -1, -1, 0) (4 args) and
returns &ret->base, not ret — adapted accordingly.

zink_context.c: zink_context_create() has two return paths — an early
return for non-threaded/compute-only contexts, and the normal
threaded-context return further down. zink_xlib_context is assigned on
both, so it's never left stale for contexts created through the
early-return path.
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
            "driver_swrast, driver_virgl, driver_asahi, driver_zink, idep_xmlconfig],",
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
            "            util_copy_rect((uint8_t*)map, pres->format, res->dt_stride, 0, 0,\n"
            "                           transfer->box.width, transfer->box.height,\n"
            "                           (const uint8_t*)res_map, transfer->stride, 0, 0);\n"
            "            pipe_texture_unmap(pctx, transfer);\n"
            "         }\n"
            "         winsys->displaytarget_unmap(winsys, res->dt);\n"
            "      }\n"
            "\n"
            "      winsys->displaytarget_display(winsys, res->dt, winsys_drawable_handle, nboxes, sub_box);\n"
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
    ],
}

# meson.build appears in nearly every directory of a Meson project, so the
# two non-root meson.build entries above need path hints to disambiguate them.
PATH_HINTS = {
    "meson.build": None,  # root only
    "meson.build:libgl-xlib": os.path.join("gallium", "targets", "libgl-xlib"),
    # A real Mesa tree also has src/gallium/frontends/va/context.c, which
    # is unrelated (VA-API frontend, not Mesa core). Its anchors would
    # never match that file's content, so it was always harmless, but
    # pin the path anyway to avoid confusing "anchor not found" noise on
    # an unrelated file.
    "context.c": os.path.join("mesa", "main"),
}


def find_files(root):
    """Return {fix_key: [full paths found]}."""
    found = {key: [] for key in FIXES}

    root_meson = os.path.join(root, "meson.build")
    if os.path.isfile(root_meson):
        found["meson.build"].append(root_meson)

    for dirpath, _, filenames in os.walk(root):
        norm_dirpath = dirpath.replace("\\", "/")
        for filename in filenames:
            if filename == "meson.build":
                hint = PATH_HINTS["meson.build:libgl-xlib"]
                if norm_dirpath.endswith(hint.replace("\\", "/")):
                    found["meson.build:libgl-xlib"].append(os.path.join(dirpath, filename))
            elif filename == "context.c":
                hint = PATH_HINTS["context.c"]
                if norm_dirpath.endswith(hint.replace("\\", "/")):
                    found["context.c"].append(os.path.join(dirpath, filename))
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
    abs_root = os.path.abspath(root)
    print(f"Searching under: {abs_root}")
    if not os.path.isdir(abs_root):
        print(f"ERROR: {abs_root} is not a directory.")
        return

    found = find_files(abs_root)
    total_found = sum(len(v) for v in found.values())
    print(f"Matched {total_found} file(s) across {len(found)} tracked filename(s).\n")

    for key, replacements in FIXES.items():
        paths = found[key]
        if not paths:
            print(f"{key}: not found under {abs_root}")
            continue
        for path in paths:
            apply_fixes(path, replacements)


if __name__ == "__main__":
    main()
