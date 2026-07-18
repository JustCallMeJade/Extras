#!/usr/bin/env python3
"""Walks a Mesa source tree, finds the files needed for xlib+freedreno
(software-blit) support, and patches them in place.

Verified against real uploaded file content:
    freedreno_context.c, freedreno_resource.c, freedreno_screen.c,
    freedreno_gmem.h, p_state.h, glx_api.c, xm_st.c
    (root meson.build, libgl-xlib meson.build, context.c, and
    inline_sw_helper.h are shared with the xlib+zink series and were
    already verified there — reused here unchanged except where noted.)

Linking fix: libgl-xlib's dependencies list now also includes
idep_xmlconfig. freedreno_screen.c calls driParseConfigFiles() and
driQueryOptionb() (pre-existing upstream code, confirmed in the real
uploaded file, not something added by this series) but the libgl-xlib
target's dependency list didn't declare idep_xmlconfig -- the library
that provides those symbols -- causing undefined-reference errors at
final link time even though every individual .c file compiles fine.
Same root cause and same fix as the equivalent issue found in the
xlib+zink script.

NOT included / deliberately left out, with reasons:

  - freedreno_resource.c's renderonly/kmsro scanout path
    (fd_resource_create_with_modifiers' `if (screen->ro && ...)` block):
    the original patch tried to disable this with NESTED /* */ comments,
    which don't nest in C — the first `*/` the compiler sees closes the
    comment early, leaving an orphaned `if (...) {` with no matching
    close and a second stray comment swallowing the real closing brace.
    As written this would fail to compile — this is the one piece that
    genuinely can't be "added in as-is" regardless of instruction, since
    it isn't valid C. The same practical effect (this target shouldn't
    hit the kmsro scanout path) is already achieved upstream instead:
    inline_sw_helper.h no longer constructs a `renderonly` struct for the
    kgsl/xlib software path, so screen->ro stays NULL and this branch is
    naturally never entered — freedreno_resource.c doesn't need touching.

Included, but adapted rather than reproduced verbatim:

  - freedreno_screen.c's driconf block: the original patch commented out
    driParseConfigFiles/driQueryOptionb entirely and hardcoded
    conservative_lrz=false, enable_throttling=false,
    dual_color_blend_by_location=true unconditionally for every freedreno
    user, not just this xlib bring-up — silently discarding real
    device-specific driconf overrides for every desktop Linux freedreno
    user too. Fixed below to gate on `config->options` being non-NULL
    instead: real users with driconf XML get identical behavior to
    upstream (parsing untouched), and only the case this bring-up
    actually needs (config->options is NULL, no driconf shipped) falls
    back to the hardcoded values the original patch wanted. Also uses the
    real driConfigFileParseParams struct-literal call signature, not the
    older positional-args one the original diff assumed.

  - freedreno_gmem.h's u_inlines.h include: added as requested. Nothing
    in the other verified fixes actually requires it, so this is a no-op
    addition, but it's harmless.

Also fixed relative to the original patch:

  - inline_sw_helper.h's gpu_fds/renderonly machinery: the original patch
    called calloc(n_drivers, n_devices) (an ~1-byte allocation, not sized
    as an int array) then read gpu_fds[0] out of it — an out-of-bounds
    read feeding directly into dup(). It also inverted its own fallback:
    on open() FAILURE it logged an error and then proceeded anyway with
    a hardcoded, meaningless fd value of 3. Given the renderonly struct
    isn't needed at all (see above), the whole gpu_fds/renderonly
    machinery is dropped; the fix below just opens /dev/kgsl-3d0 and
    calls fd_screen_create() with a NULL renderonly, bailing out (screen
    stays NULL) on open failure instead of guessing a fd.

  - p_state.h: dt1/dt_stride are inserted before the `screen` field, not
    after it as the original patch did — the field immediately above
    screen is explicitly commented "The screen pointer should be last
    for optimal structure packing," and the patch's placement broke that.

  - xm_st.c: the original patch replaced the shared
    xstfb->screen->flush_frontbuffer(...) call unconditionally with a
    freedreno-specific function. xm_st.c is the generic xlib GLX frontend
    used by every gallium backend that renders through xlib (softpipe,
    llvmpipe, virgl, zink, now freedreno) — replacing this call site
    outright breaks frontbuffer presentation for all of them. Fixed
    below to branch on whether freedreno_winsys is actually set instead.

  - freedreno_context.c: fd_context_init_tc() has TWO early-return paths
    (non-threaded, and compute-only) in addition to its final return —
    the original patch only assigned freedreno_xlib_context before the
    final return, leaving both early-return paths able to produce a
    context that freedreno_xlib_context never points to (same class of
    bug found and fixed in the zink series' zink_context_create()). All
    three return points are covered below.
"""

import os
import sys

FIXES = {
    "meson.build": [
        (
            "    elif not with_gallium_swrast\n"
            "      error('xlib based GLX requires softpipe or llvmpipe.')",
            "    elif not with_gallium_swrast and not with_gallium_freedreno\n"
            "      error('xlib based GLX requires softpipe, llvmpipe, or freedreno.')",
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
            '#ifdef GALLIUM_FREEDRENO\n'
            '#include "freedreno/freedreno_screen.h"\n'
            '#include <fcntl.h>\n'
            '\n'
            'struct sw_winsys *freedreno_winsys;\n'
            '#endif',
        ),
        (
            "#if defined(GALLIUM_D3D12)\n"
            "   if (screen == NULL && strcmp(driver, \"d3d12\") == 0)\n"
            "      screen = d3d12_create_dxcore_screen(winsys, NULL);\n"
            "#endif\n"
            "\n"
            "   return screen ? debug_screen_wrap(screen) : NULL;",
            "#if defined(GALLIUM_D3D12)\n"
            "   if (screen == NULL && strcmp(driver, \"d3d12\") == 0)\n"
            "      screen = d3d12_create_dxcore_screen(winsys, NULL);\n"
            "#endif\n"
            "\n"
            "#if defined(GALLIUM_FREEDRENO)\n"
            "   if (screen == NULL && strcmp(driver, \"freedreno\") == 0) {\n"
            "      int fd = open(\"/dev/kgsl-3d0\", O_RDWR | O_CLOEXEC | O_NONBLOCK);\n"
            "      if (fd >= 0) {\n"
            "         struct pipe_screen_config dummy_cfg = { NULL, NULL };\n"
            "         freedreno_winsys = winsys;\n"
            "         /* no renderonly device on kgsl: this target has no KMS/DRM\n"
            "          * scanout to hand to renderonly, so pass NULL and rely on\n"
            "          * the sw_winsys software-blit display path instead. */\n"
            "         screen = fd_screen_create(fd, &dummy_cfg, NULL);\n"
            "      }\n"
            "   }\n"
            "#endif\n"
            "\n"
            "   return screen ? debug_screen_wrap(screen) : NULL;",
        ),
    ],
    "meson.build:libgl-xlib": [
        (
            "dependencies : [dep_x11, idep_mesautil, dep_thread, dep_clock, dep_unwind, "
            "driver_swrast, driver_virgl, driver_asahi],",
            "dependencies : [dep_x11, idep_mesautil, dep_thread, dep_clock, dep_unwind, "
            "driver_swrast, driver_virgl, driver_asahi, driver_freedreno, idep_xmlconfig],",
        ),
    ],
    "freedreno_context.c": [
        (
            '#include "freedreno_tracepoints.h"\n'
            '#include "util/u_trace_gallium.h"\n',
            '#include "freedreno_tracepoints.h"\n'
            '#include "util/u_trace_gallium.h"\n'
            '\n'
            'struct pipe_context* freedreno_xlib_context;\n',
        ),
        (
            "   fd_autotune_init(&ctx->autotune, screen->dev);\n"
            "\n"
            "   if (!(ctx->flags & FD_CONTEXT_FLAG_AUX))\n"
            "      p_atomic_inc(&pctx->screen->num_contexts);\n"
            "\n"
            "   return pctx;\n"
            "\n"
            "fail:",
            "   fd_autotune_init(&ctx->autotune, screen->dev);\n"
            "\n"
            "   if (!(ctx->flags & FD_CONTEXT_FLAG_AUX))\n"
            "      p_atomic_inc(&pctx->screen->num_contexts);\n"
            "\n"
            "   freedreno_xlib_context = pctx;\n"
            "\n"
            "   return pctx;\n"
            "\n"
            "fail:",
        ),
        (
            "   if (!(flags & PIPE_CONTEXT_PREFER_THREADED))\n"
            "      return pctx;\n"
            "\n"
            "   /* Clover (compute-only) is unsupported. */\n"
            "   if (flags & PIPE_CONTEXT_COMPUTE_ONLY)\n"
            "      return pctx;",
            "   if (!(flags & PIPE_CONTEXT_PREFER_THREADED)) {\n"
            "      freedreno_xlib_context = pctx;\n"
            "      return pctx;\n"
            "   }\n"
            "\n"
            "   /* Clover (compute-only) is unsupported. */\n"
            "   if (flags & PIPE_CONTEXT_COMPUTE_ONLY) {\n"
            "      freedreno_xlib_context = pctx;\n"
            "      return pctx;\n"
            "   }",
        ),
        (
            "   if (tc && tc != pctx) {\n"
            "      threaded_context_init_bytes_mapped_limit((struct threaded_context *)tc, 16);\n"
            "      ((struct threaded_context *)tc)->bytes_replaced_limit =\n"
            "         ((struct threaded_context *)tc)->bytes_mapped_limit / 4;\n"
            "   }\n"
            "\n"
            "   return tc;\n"
            "}",
            "   if (tc && tc != pctx) {\n"
            "      threaded_context_init_bytes_mapped_limit((struct threaded_context *)tc, 16);\n"
            "      ((struct threaded_context *)tc)->bytes_replaced_limit =\n"
            "         ((struct threaded_context *)tc)->bytes_mapped_limit / 4;\n"
            "   }\n"
            "\n"
            "   freedreno_xlib_context = tc;\n"
            "\n"
            "   return tc;\n"
            "}",
        ),
        (
            "pctx->get_device_reset_status = fd_get_device_reset_status_direct;",
            "pctx->get_device_reset_status = fd_get_device_reset_status;",
        ),
        (
            "fd_get_device_reset_status_direct;",
            "fd_get_device_reset_status;",
        ),
    ],
    "freedreno_resource.c": [
        (
            '#include "frontend/drm_driver.h"\n',
            '#include "frontend/drm_driver.h"\n'
            '\n'
            '#include "frontend/sw_winsys.h"\n'
            'extern struct sw_winsys *freedreno_winsys;\n',
        ),
        (
            "   /* Hand out the resolved size. */\n"
            "   if (psize)\n"
            "      *psize = size;\n"
            "\n"
            "   return prsc;\n"
            "}",
            "   /* Hand out the resolved size. */\n"
            "   if (psize)\n"
            "      *psize = size;\n"
            "\n"
            "   if (freedreno_winsys && (tmpl->bind & PIPE_BIND_DISPLAY_TARGET)) {\n"
            "      prsc->dt1 = freedreno_winsys->displaytarget_create(freedreno_winsys,\n"
            "                                                        prsc->bind,\n"
            "                                                        prsc->format,\n"
            "                                                        tmpl->width0,\n"
            "                                                        tmpl->height0,\n"
            "                                                        64, NULL,\n"
            "                                                        &prsc->dt_stride);\n"
            "   }\n"
            "\n"
            "   return prsc;\n"
            "}",
        ),
        (
            "   rsc->valid = true;\n"
            "\n"
            "   if (FD_DBG(LAYOUT))\n"
            "      fdl_dump_layout(&rsc->layout);\n"
            "\n"
            "   return prsc;\n"
            "\n"
            "fail:",
            "   rsc->valid = true;\n"
            "\n"
            "   if (freedreno_winsys && (tmpl->bind & PIPE_BIND_DISPLAY_TARGET)) {\n"
            "      prsc->dt1 = freedreno_winsys->displaytarget_create(freedreno_winsys,\n"
            "                                                        prsc->bind,\n"
            "                                                        prsc->format,\n"
            "                                                        tmpl->width0,\n"
            "                                                        tmpl->height0,\n"
            "                                                        64, NULL,\n"
            "                                                        &prsc->dt_stride);\n"
            "   }\n"
            "\n"
            "   if (FD_DBG(LAYOUT))\n"
            "      fdl_dump_layout(&rsc->layout);\n"
            "\n"
            "   return prsc;\n"
            "\n"
            "fail:",
        ),
    ],
    "freedreno_screen.c": [
        (
            "   /* parse driconf configuration now for device specific overrides: */\n"
            "   driParseConfigFiles(config->options, config->options_info,\n"
            "                       &(driConfigFileParseParams) {\n"
            "                          .driverName = \"msm\",\n"
            "                          .deviceName = fd_dev_name(screen->dev_id),\n"
            "                       });\n"
            "\n"
            "   screen->driconf.heap_memory_percent =\n"
            "         driQueryOptionf(config->options, \"heap_memory_percent\");\n"
            "   screen->driconf.conservative_lrz =\n"
            "         !driQueryOptionb(config->options, \"disable_conservative_lrz\");\n"
            "   screen->driconf.enable_throttling =\n"
            "         !driQueryOptionb(config->options, \"disable_throttling\");\n"
            "   screen->driconf.dual_color_blend_by_location =\n"
            "         driQueryOptionb(config->options, \"dual_color_blend_by_location\");\n"
            "   if (driQueryOptionb(config->options, \"disable_explicit_sync_heuristic\"))\n"
            "      fd_device_disable_explicit_sync_heuristic(dev);\n",
            "   /* parse driconf configuration now for device specific overrides,\n"
            "    * where available (this target may not ship any driconf XML) */\n"
            "   if (config->options) {\n"
            "      driParseConfigFiles(config->options, config->options_info,\n"
            "                          &(driConfigFileParseParams) {\n"
            "                             .driverName = \"msm\",\n"
            "                             .deviceName = fd_dev_name(screen->dev_id),\n"
            "                          });\n"
            "\n"
            "      screen->driconf.heap_memory_percent =\n"
            "            driQueryOptionf(config->options, \"heap_memory_percent\");\n"
            "      screen->driconf.conservative_lrz =\n"
            "            !driQueryOptionb(config->options, \"disable_conservative_lrz\");\n"
            "      screen->driconf.enable_throttling =\n"
            "            !driQueryOptionb(config->options, \"disable_throttling\");\n"
            "      screen->driconf.dual_color_blend_by_location =\n"
            "            driQueryOptionb(config->options, \"dual_color_blend_by_location\");\n"
            "      if (driQueryOptionb(config->options, \"disable_explicit_sync_heuristic\"))\n"
            "         fd_device_disable_explicit_sync_heuristic(dev);\n"
            "   } else {\n"
            "      screen->driconf.conservative_lrz = false;\n"
            "      screen->driconf.enable_throttling = false;\n"
            "      screen->driconf.dual_color_blend_by_location = true;\n"
            "   }\n",
        ),
    ],
    "freedreno_gmem.h": [
        (
            '#include "pipe/p_state.h"\n'
            '#include "util/list.h"\n',
            '#include "pipe/p_state.h"\n'
            '#include "util/list.h"\n'
            '#include "util/u_inlines.h"\n',
        ),
    ],
    "p_state.h": [
        (
            "   struct pipe_resource *next;\n"
            "   /* The screen pointer should be last for optimal structure packing.\n"
            "    * This pointer cannot be casted directly to a driver's screen. Use\n"
            "    * screen::get_driver_pipe_screen instead if it's non-NULL.\n"
            "    */\n"
            "   struct pipe_screen *screen; /**< screen that this texture belongs to */\n"
            "};",
            "   struct pipe_resource *next;\n"
            "\n"
            "   struct sw_displaytarget *dt1;\n"
            "   unsigned dt_stride;\n"
            "\n"
            "   /* The screen pointer should be last for optimal structure packing.\n"
            "    * This pointer cannot be casted directly to a driver's screen. Use\n"
            "    * screen::get_driver_pipe_screen instead if it's non-NULL.\n"
            "    */\n"
            "   struct pipe_screen *screen; /**< screen that this texture belongs to */\n"
            "};",
        ),
    ],
    "glx_api.c": [
        (
            "   XMesaVisual xmvis = (XMesaVisual) config;\n"
            "   XMesaBuffer xmbuf;\n"
            "   if (!xmvis)\n"
            "      return 0;\n"
            "\n"
            "   xmbuf = XMesaCreateWindowBuffer(xmvis, win);\n"
            "   if (!xmbuf)\n"
            "      return 0;\n"
            "\n"
            "   (void) dpy;",
            "   XMesaVisual xmvis = (XMesaVisual) config;\n"
            "   XMesaBuffer xmbuf;\n"
            "   if (!xmvis)\n"
            "      return 0;\n"
            "\n"
            "   xmbuf = XMesaCreateWindowBuffer(xmvis, win);\n"
            "   if (!xmbuf)\n"
            "      return 0;\n"
            "\n"
            "   {\n"
            "      const unsigned char value = 0;\n"
            "      Atom var1 = XInternAtom(dpy, \"_MESA_DRV\", 0);\n"
            "      XChangeProperty(dpy, win, var1, 6, 8, 0, &value, 1);\n"
            "   }\n"
            "\n"
            "   (void) dpy;",
        ),
    ],
    "xm_st.c": [
        (
            '#include "state_tracker/st_context.h"\n',
            '#include "state_tracker/st_context.h"\n'
            '#include "frontend/sw_winsys.h"\n'
            '\n'
            'extern struct pipe_context* freedreno_xlib_context;\n'
            'extern struct sw_winsys *freedreno_winsys;\n'
            '\n'
            'static void\n'
            'freedreno_flush_frontbuffer(struct pipe_screen *pscreen,\n'
            '                            struct pipe_context *pctx,\n'
            '                            struct pipe_resource *pres,\n'
            '                            unsigned level, unsigned layer,\n'
            '                            void *winsys_drawable_handle,\n'
            '                            unsigned nboxes,\n'
            '                            struct pipe_box *sub_box)\n'
            '{\n'
            '   pctx = freedreno_xlib_context;\n'
            '\n'
            '   void *map = freedreno_winsys->displaytarget_map(freedreno_winsys, pres->dt1, 0);\n'
            '\n'
            '   if (map) {\n'
            '      struct pipe_transfer *transfer = NULL;\n'
            '\n'
            '      void *res_map = pipe_texture_map(pctx, pres, level, layer, PIPE_MAP_READ, 0, 0,\n'
            '                                        u_minify(pres->width0, level),\n'
            '                                        u_minify(pres->height0, level),\n'
            '                                        &transfer);\n'
            '      if (res_map) {\n'
            '         util_copy_rect((uint8_t*)map, pres->format, pres->dt_stride, 0, 0,\n'
            '                        transfer->box.width, transfer->box.height,\n'
            '                        (const uint8_t*)res_map, transfer->stride, 0, 0);\n'
            '         pipe_texture_unmap(pctx, transfer);\n'
            '      }\n'
            '      freedreno_winsys->displaytarget_unmap(freedreno_winsys, pres->dt1);\n'
            '   }\n'
            '\n'
            '   freedreno_winsys->displaytarget_display(freedreno_winsys, pres->dt1, winsys_drawable_handle, nboxes, sub_box);\n'
            '}\n',
        ),
        (
            "   xstfb->screen->flush_frontbuffer(xstfb->screen, pctx, pres, 0, 0, &xstfb->buffer->ws, nboxes, box);\n"
            "   return true;",
            "   if (freedreno_winsys)\n"
            "      freedreno_flush_frontbuffer(xstfb->screen, pctx, pres, 0, 0, &xstfb->buffer->ws, nboxes, box);\n"
            "   else\n"
            "      xstfb->screen->flush_frontbuffer(xstfb->screen, pctx, pres, 0, 0, &xstfb->buffer->ws, nboxes, box);\n"
            "   return true;",
        ),
    ],
}

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
