"""Visually crop a GIF (or still image) with a fixed square selection.

The crop box always keeps a 1:1 aspect ratio. Export sizes include 80x80 and
240x240 presets, plus a user-supplied custom width and height.
"""

import os
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageSequence, ImageTk


MIN_CROP_PX = 16
HANDLE_HIT_PX = 14
HANDLE_DRAW_PX = 8
PREVIEW_SIZE = 560
MAX_UPSCALE = 4.0
MAX_OUTPUT_PX = 8192

OPEN_FILETYPES = [
    ("GIF and images", "*.gif *.png *.jpg *.jpeg *.webp *.bmp"),
    ("GIF files", "*.gif"),
    ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp"),
    ("All files", "*.*"),
]

FORMAT_BY_EXT = {
    ".gif": "GIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".bmp": "BMP",
}

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

# (label, radio value, output size in pixels)
SIZE_PRESETS = [
    ("80 x 80", "80", (80, 80)),
    ("240 x 240", "240", (240, 240)),
]


def largest_centered_square(image_w, image_h):
    """Return (x, y, side) for the biggest square centred in the image."""
    side = min(image_w, image_h)
    return (image_w - side) // 2, (image_h - side) // 2, side


def clamp_box(x, y, side, image_w, image_h):
    """Clamp a square crop box so it stays fully inside the image."""
    smallest = min(MIN_CROP_PX, image_w, image_h)
    side = max(smallest, min(int(round(side)), image_w, image_h))
    x = max(0, min(int(round(x)), image_w - side))
    y = max(0, min(int(round(y)), image_h - side))
    return x, y, side


def _drag_side(raw_side, limit):
    limit = int(limit)
    if limit < 1:
        return 0
    lower = min(MIN_CROP_PX, limit)
    return max(lower, min(int(round(raw_side)), limit))


def corner_box(corner, anchor_x, anchor_y, pointer_x, pointer_y, image_w, image_h):
    """Return a square crop box while dragging one corner.

    (anchor_x, anchor_y) is the fixed opposite corner in image coordinates.
    """
    anchor_x = int(anchor_x)
    anchor_y = int(anchor_y)

    if corner == "se":
        side = _drag_side(
            max(pointer_x - anchor_x, pointer_y - anchor_y),
            min(image_w - anchor_x, image_h - anchor_y),
        )
        return anchor_x, anchor_y, side

    if corner == "nw":
        side = _drag_side(
            max(anchor_x - pointer_x, anchor_y - pointer_y),
            min(anchor_x, anchor_y),
        )
        return anchor_x - side, anchor_y - side, side

    if corner == "ne":
        side = _drag_side(
            max(pointer_x - anchor_x, anchor_y - pointer_y),
            min(image_w - anchor_x, anchor_y),
        )
        return anchor_x, anchor_y - side, side

    if corner == "sw":
        side = _drag_side(
            max(anchor_x - pointer_x, pointer_y - anchor_y),
            min(anchor_x, image_h - anchor_y),
        )
        return anchor_x - side, anchor_y, side

    raise ValueError(f"Unknown corner: {corner}")


def _safe_int(value, fallback):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _crop_and_resize(frame, crop_box, output_size):
    crop_x, crop_y, side = crop_box
    out_w, out_h = output_size
    rgba = frame.convert("RGBA")
    cropped = rgba.crop((crop_x, crop_y, crop_x + side, crop_y + side))
    if cropped.size != (out_w, out_h):
        cropped = cropped.resize((out_w, out_h), RESAMPLE)
    return cropped


def _flatten(image):
    """Composite transparency onto white so JPEG/BMP exports do not go black."""
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    background.alpha_composite(image.convert("RGBA"))
    return background.convert("RGB")


def _save_gif(frames, durations, loop, path):
    frames[0].save(
        path,
        format="GIF",
        save_all=len(frames) > 1,
        append_images=frames[1:],
        duration=durations if len(frames) > 1 else durations[0],
        loop=loop,
        disposal=2,
        optimize=True,
    )


def _save_still(image, path, fmt):
    if fmt == "JPEG":
        _flatten(image).save(path, format="JPEG", quality=95, optimize=True)
    elif fmt == "BMP":
        _flatten(image).save(path, format="BMP")
    elif fmt == "WEBP":
        image.save(path, format="WEBP", quality=95)
    else:
        image.save(path, format=fmt)


def export_cropped(input_path, output_path, crop_box, output_size, fmt):
    """Crop and resize the source and write the result to output_path.

    GIF output keeps every frame; other formats receive a single frame. Returns
    the number of frames written. The file is written atomically so a failed
    export does not leave a truncated image behind.
    """
    output_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    suffix = os.path.splitext(output_path)[1] or ".tmp"
    fd, temp_path = tempfile.mkstemp(prefix=".giftools-", suffix=suffix, dir=output_dir)
    os.close(fd)

    try:
        with Image.open(input_path) as image:
            frame_count = getattr(image, "n_frames", 1)
            default_duration = _safe_int(image.info.get("duration"), 100)
            loop = _safe_int(image.info.get("loop"), 0)

            if frame_count == 1 and image.format in ("JPEG", "MPO", "TIFF"):
                image = ImageOps.exif_transpose(image)

            frames = []
            durations = []
            for frame in ImageSequence.Iterator(image):
                durations.append(max(1, _safe_int(frame.info.get("duration"), default_duration)))
                frames.append(_crop_and_resize(frame, crop_box, output_size))

            if not frames:
                raise ValueError("No frames found in the source file.")

            if fmt == "GIF":
                _save_gif(frames, durations, loop, temp_path)
                written = len(frames)
            else:
                _save_still(frames[0], temp_path, fmt)
                written = 1

        os.replace(temp_path, output_path)
        return written
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


class CropResizeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("GIF Crop & Resize")
        try:
            self.root.wm_attributes("-toolwindow", True)
        except tk.TclError:
            pass  # Windows-only attribute; harmless elsewhere.

        self.input_path = None
        self.open_dir = os.path.expanduser("~")
        self.source_image = None
        self.source_size = (0, 0)
        self.frame_count = 1
        self.is_animated = False
        self.zoom = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.crop = (0, 0, 1)
        self.tk_preview = None

        self.drag_mode = None
        self.drag_corner = None
        self.anchor = (0, 0)
        self.move_offset = (0, 0)

        self.size_choice = tk.StringVar(value=SIZE_PRESETS[0][1])
        self.custom_width = tk.StringVar(value="320")
        self.custom_height = tk.StringVar(value="320")
        self.keep_square = tk.BooleanVar(value=True)
        self.custom_syncing = False

        self.info_var = tk.StringVar(value="No file loaded")
        self.crop_var = tk.StringVar(value="")
        self.hint_var = tk.StringVar(
            value="The crop box is locked to a square. Drag inside it to move, "
            "drag a corner to resize, or nudge it with the arrow keys."
        )

        self._build_ui()
        self._bind_events()
        self._update_crop_label()

    def _build_ui(self):
        outer = tk.Frame(self.root)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        controls = tk.Frame(outer)
        controls.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

        tk.Button(controls, text="Open GIF or image...", command=self.load_file).pack(fill=tk.X)
        tk.Label(
            controls, textvariable=self.info_var, justify=tk.LEFT, wraplength=250
        ).pack(fill=tk.X, pady=(6, 0))

        ttk.Separator(controls, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=8)

        crop_frame = tk.LabelFrame(controls, text="Square crop")
        crop_frame.pack(fill=tk.X)
        tk.Label(
            crop_frame, textvariable=self.crop_var, justify=tk.LEFT, wraplength=230
        ).pack(fill=tk.X, padx=6, pady=(4, 2))
        tk.Label(
            crop_frame,
            textvariable=self.hint_var,
            justify=tk.LEFT,
            wraplength=230,
            fg="#555555",
        ).pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Button(crop_frame, text="Reset to centred square", command=self.reset_crop).pack(
            fill=tk.X, padx=6, pady=(0, 6)
        )

        size_frame = tk.LabelFrame(controls, text="Export size")
        size_frame.pack(fill=tk.X, pady=(8, 0))

        for label, value, _size in SIZE_PRESETS:
            tk.Radiobutton(
                size_frame,
                text=label,
                value=value,
                variable=self.size_choice,
                command=self._on_size_choice,
            ).pack(anchor="w", padx=6)

        tk.Radiobutton(
            size_frame,
            text="Custom",
            value="custom",
            variable=self.size_choice,
            command=self._on_size_choice,
        ).pack(anchor="w", padx=6)

        custom_frame = tk.Frame(size_frame)
        custom_frame.pack(fill=tk.X, padx=(24, 6), pady=(0, 6))

        tk.Label(custom_frame, text="W:").grid(row=0, column=0, sticky="w")
        self.width_spin = tk.Spinbox(
            custom_frame,
            from_=1,
            to=MAX_OUTPUT_PX,
            width=7,
            textvariable=self.custom_width,
        )
        self.width_spin.grid(row=0, column=1, padx=(2, 8), pady=2, sticky="w")

        tk.Label(custom_frame, text="H:").grid(row=0, column=2, sticky="w")
        self.height_spin = tk.Spinbox(
            custom_frame,
            from_=1,
            to=MAX_OUTPUT_PX,
            width=7,
            textvariable=self.custom_height,
        )
        self.height_spin.grid(row=0, column=3, padx=(2, 0), pady=2, sticky="w")

        self.square_check = tk.Checkbutton(
            custom_frame,
            text="Keep square",
            variable=self.keep_square,
            command=self._on_keep_square,
        )
        self.square_check.grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))
        tk.Label(
            custom_frame,
            text="Uncheck Keep square to stretch the crop.",
            fg="#555555",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        self.width_spin.configure(state="disabled")
        self.height_spin.configure(state="disabled")
        self.square_check.configure(state="disabled")

        tk.Button(controls, text="Export...", command=self.export).pack(fill=tk.X, pady=(10, 0))

        preview_frame = tk.Frame(outer, bd=2, relief=tk.SUNKEN)
        preview_frame.pack(side=tk.RIGHT, padx=(8, 0))

        self.canvas = tk.Canvas(
            preview_frame,
            width=PREVIEW_SIZE,
            height=PREVIEW_SIZE,
            bg="#2b2b2b",
            highlightthickness=0,
            takefocus=True,
        )
        self.canvas.pack()
        self._draw_placeholder()

    def _bind_events(self):
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Left>", lambda event: self._nudge(-1, 0, event))
        self.canvas.bind("<Right>", lambda event: self._nudge(1, 0, event))
        self.canvas.bind("<Up>", lambda event: self._nudge(0, -1, event))
        self.canvas.bind("<Down>", lambda event: self._nudge(0, 1, event))

        self.custom_width.trace_add("write", self._on_custom_width_changed)
        self.custom_height.trace_add("write", self._on_custom_height_changed)

    def _draw_placeholder(self):
        self.canvas.delete("all")
        self.canvas.create_text(
            PREVIEW_SIZE / 2,
            PREVIEW_SIZE / 2,
            text="Open a GIF or image to start",
            fill="#bbbbbb",
        )

    def load_file(self):
        initial_dir = self.open_dir
        if not os.path.isdir(initial_dir):
            initial_dir = os.path.expanduser("~")
        path = filedialog.askopenfilename(
            title="Open GIF or image",
            filetypes=OPEN_FILETYPES,
            initialdir=initial_dir,
        )
        if path:
            self.load_path(path)

    def load_path(self, path):
        try:
            with Image.open(path) as image:
                frame_count = getattr(image, "n_frames", 1)
                if frame_count == 1 and image.format in ("JPEG", "MPO", "TIFF"):
                    image = ImageOps.exif_transpose(image)
                preview = image.convert("RGBA").copy()
        except Exception as exc:
            messagebox.showerror("Could not open file", f"{path}\n\n{exc}")
            return

        self.input_path = path
        self.open_dir = os.path.dirname(os.path.abspath(path))
        self.source_image = preview
        self.source_size = preview.size
        self.frame_count = frame_count
        self.is_animated = frame_count > 1

        self.zoom = min(
            PREVIEW_SIZE / preview.width,
            PREVIEW_SIZE / preview.height,
            MAX_UPSCALE,
        )
        display_w = max(1, int(round(preview.width * self.zoom)))
        display_h = max(1, int(round(preview.height * self.zoom)))
        self.offset_x = (PREVIEW_SIZE - display_w) // 2
        self.offset_y = (PREVIEW_SIZE - display_h) // 2

        resized = preview.resize((display_w, display_h), RESAMPLE)
        self.tk_preview = ImageTk.PhotoImage(resized)

        self.crop = largest_centered_square(*preview.size)
        self._update_info()
        self._update_crop_label()
        self._redraw()
        self.canvas.focus_set()

    def reset_crop(self):
        if self.source_image is None:
            return
        self.crop = largest_centered_square(*self.source_size)
        self._update_crop_label()
        self._redraw()

    def _update_info(self):
        width, height = self.source_size
        if self.frame_count == 1:
            kind = "still image"
        else:
            kind = f"{self.frame_count} frames"
        self.info_var.set(
            f"{os.path.basename(self.input_path)}\n{width} x {height} px - {kind}"
        )

    def _output_size(self):
        choice = self.size_choice.get()
        for _label, value, size in SIZE_PRESETS:
            if choice == value:
                return size

        try:
            width = int(self.custom_width.get())
            height = int(self.custom_height.get())
        except (TypeError, ValueError):
            return None

        if not (1 <= width <= MAX_OUTPUT_PX and 1 <= height <= MAX_OUTPUT_PX):
            return None
        return width, height

    def _update_crop_label(self):
        if self.source_image is None:
            self.crop_var.set("No crop yet")
            return

        side = self.crop[2]
        output = self._output_size()
        if output is None:
            self.crop_var.set(f"Crop: {side} x {side} px\nExport: custom size")
        else:
            self.crop_var.set(
                f"Crop: {side} x {side} px\nExport: {output[0]} x {output[1]} px"
            )

    def _on_size_choice(self):
        custom = self.size_choice.get() == "custom"
        self.width_spin.configure(state="normal" if custom else "disabled")
        self.height_spin.configure(
            state="disabled" if (not custom or self.keep_square.get()) else "normal"
        )
        self.square_check.configure(state="normal" if custom else "disabled")
        if custom and self.keep_square.get():
            self._sync_custom_height()
        self._update_crop_label()

    def _on_keep_square(self):
        custom = self.size_choice.get() == "custom"
        self.height_spin.configure(
            state="disabled" if (custom and self.keep_square.get()) else "normal"
        )
        if custom and self.keep_square.get():
            self._sync_custom_height()
        self._update_crop_label()

    def _sync_custom_height(self):
        if self.custom_syncing:
            return
        self.custom_syncing = True
        try:
            self.custom_height.set(self.custom_width.get())
        finally:
            self.custom_syncing = False

    def _on_custom_width_changed(self, *_):
        if self.size_choice.get() == "custom" and self.keep_square.get():
            self._sync_custom_height()
        self._update_crop_label()

    def _on_custom_height_changed(self, *_):
        if self.custom_syncing:
            return
        if self.size_choice.get() == "custom" and self.keep_square.get():
            self.custom_syncing = True
            try:
                self.custom_width.set(self.custom_height.get())
            finally:
                self.custom_syncing = False
        self._update_crop_label()

    def _canvas_to_image(self, x, y):
        return (
            (x - self.offset_x) / self.zoom,
            (y - self.offset_y) / self.zoom,
        )

    def _crop_canvas_box(self):
        crop_x, crop_y, side = self.crop
        x0 = self.offset_x + crop_x * self.zoom
        y0 = self.offset_y + crop_y * self.zoom
        x1 = self.offset_x + (crop_x + side) * self.zoom
        y1 = self.offset_y + (crop_y + side) * self.zoom
        return x0, y0, x1, y1

    def _handle_at(self, x, y):
        x0, y0, x1, y1 = self._crop_canvas_box()
        handles = {
            "nw": (x0, y0),
            "ne": (x1, y0),
            "sw": (x0, y1),
            "se": (x1, y1),
        }

        best = None
        best_distance = HANDLE_HIT_PX
        for corner, (hx, hy) in handles.items():
            distance = max(abs(x - hx), abs(y - hy))
            if distance <= best_distance:
                best = corner
                best_distance = distance
        return best

    def _inside_crop(self, x, y):
        x0, y0, x1, y1 = self._crop_canvas_box()
        return x0 <= x <= x1 and y0 <= y <= y1

    def _set_cursor(self, *names):
        """Apply the first cursor name supported by this platform."""
        for name in names:
            try:
                self.canvas.configure(cursor=name)
                return
            except tk.TclError:
                continue
        self.canvas.configure(cursor="")

    def _set_handle_cursor(self, corner):
        if corner in ("nw", "se"):
            self._set_cursor("size_nw_se", "bottom_right_corner", "sizing")
        elif corner in ("ne", "sw"):
            self._set_cursor("size_ne_sw", "top_right_corner", "sizing")
        else:
            self._set_cursor("")

    def _on_press(self, event):
        self.canvas.focus_set()
        if self.source_image is None:
            return

        corner = self._handle_at(event.x, event.y)
        if corner:
            self.drag_mode = "corner"
            self.drag_corner = corner
            x, y, side = self.crop
            if corner == "nw":
                self.anchor = (x + side, y + side)
            elif corner == "ne":
                self.anchor = (x, y + side)
            elif corner == "sw":
                self.anchor = (x + side, y)
            else:
                self.anchor = (x, y)
            self._set_handle_cursor(corner)
            return

        if self._inside_crop(event.x, event.y):
            self.drag_mode = "move"
            crop_x, crop_y, _ = self.crop
            image_x, image_y = self._canvas_to_image(event.x, event.y)
            self.move_offset = (image_x - crop_x, image_y - crop_y)
            self._set_cursor("fleur")
            return

        self.drag_mode = None

    def _on_drag(self, event):
        if self.source_image is None or self.drag_mode is None:
            return

        if self.drag_mode == "move":
            image_x, image_y = self._canvas_to_image(event.x, event.y)
            new_x = image_x - self.move_offset[0]
            new_y = image_y - self.move_offset[1]
            self.crop = clamp_box(
                new_x, new_y, self.crop[2], self.source_size[0], self.source_size[1]
            )
        else:
            image_x, image_y = self._canvas_to_image(event.x, event.y)
            self.crop = corner_box(
                self.drag_corner,
                self.anchor[0],
                self.anchor[1],
                image_x,
                image_y,
                self.source_size[0],
                self.source_size[1],
            )

        self._update_crop_label()
        self._redraw()

    def _on_release(self, _event):
        self.drag_mode = None
        self.drag_corner = None
        self._set_cursor("")

    def _on_hover(self, event):
        if self.source_image is None or self.drag_mode:
            return

        corner = self._handle_at(event.x, event.y)
        if corner:
            self._set_handle_cursor(corner)
        elif self._inside_crop(event.x, event.y):
            self._set_cursor("fleur")
        else:
            self._set_cursor("")

    def _nudge(self, dx, dy, event):
        if self.source_image is None:
            return
        step = 10 if (event.state & 0x0001) else 1
        x, y, side = self.crop
        self.crop = clamp_box(
            x + dx * step,
            y + dy * step,
            side,
            self.source_size[0],
            self.source_size[1],
        )
        self._update_crop_label()
        self._redraw()

    def _redraw(self):
        self.canvas.delete("all")
        if self.source_image is None:
            self._draw_placeholder()
            return

        self.canvas.create_image(
            self.offset_x, self.offset_y, anchor="nw", image=self.tk_preview
        )

        display_w = max(1, int(round(self.source_size[0] * self.zoom)))
        display_h = max(1, int(round(self.source_size[1] * self.zoom)))
        image_x1 = self.offset_x + display_w
        image_y1 = self.offset_y + display_h

        crop_x0, crop_y0, crop_x1, crop_y1 = self._crop_canvas_box()
        dim = {"fill": "#000000", "stipple": "gray50", "outline": ""}

        # Dim everything outside the crop box.
        self.canvas.create_rectangle(
            self.offset_x, self.offset_y, image_x1, crop_y0, **dim
        )
        self.canvas.create_rectangle(
            self.offset_x, crop_y1, image_x1, image_y1, **dim
        )
        self.canvas.create_rectangle(
            self.offset_x, crop_y0, crop_x0, crop_y1, **dim
        )
        self.canvas.create_rectangle(crop_x1, crop_y0, image_x1, crop_y1, **dim)

        # Rule-of-thirds guides.
        for step in (1, 2):
            guide_x = crop_x0 + (crop_x1 - crop_x0) * step / 3
            guide_y = crop_y0 + (crop_y1 - crop_y0) * step / 3
            self.canvas.create_line(
                guide_x, crop_y0, guide_x, crop_y1, fill="#ffffff", stipple="gray50"
            )
            self.canvas.create_line(
                crop_x0, guide_y, crop_x1, guide_y, fill="#ffffff", stipple="gray50"
            )

        self.canvas.create_rectangle(
            crop_x0, crop_y0, crop_x1, crop_y1, outline="#4ade80", width=2
        )

        half = HANDLE_DRAW_PX / 2
        for handle_x, handle_y in (
            (crop_x0, crop_y0),
            (crop_x1, crop_y0),
            (crop_x0, crop_y1),
            (crop_x1, crop_y1),
        ):
            self.canvas.create_rectangle(
                handle_x - half,
                handle_y - half,
                handle_x + half,
                handle_y + half,
                fill="#4ade80",
                outline="#14532d",
            )

    def export(self):
        if self.source_image is None:
            messagebox.showwarning("No file", "Open a GIF or image first.")
            return

        output_size = self._output_size()
        if output_size is None:
            messagebox.showerror(
                "Invalid size",
                f"Enter a width and height between 1 and {MAX_OUTPUT_PX} pixels.",
            )
            return

        source_ext = os.path.splitext(self.input_path)[1].lower()
        if self.is_animated:
            default_ext = ".gif"
            filetypes = [("GIF files", "*.gif")]
        elif source_ext == ".gif":
            default_ext = ".gif"
            filetypes = [
                ("GIF files", "*.gif"),
                ("PNG image", "*.png"),
                ("WebP image", "*.webp"),
                ("JPEG image", "*.jpg *.jpeg"),
            ]
        else:
            default_ext = ".png"
            filetypes = [
                ("PNG image", "*.png"),
                ("GIF files", "*.gif"),
                ("WebP image", "*.webp"),
                ("JPEG image", "*.jpg *.jpeg"),
            ]

        base_name = os.path.splitext(os.path.basename(self.input_path))[0]
        initial_name = f"{base_name}_{output_size[0]}x{output_size[1]}{default_ext}"
        dialog_options = {
            "title": "Export cropped image",
            "defaultextension": default_ext,
            "initialfile": initial_name,
            "filetypes": filetypes,
        }
        source_dir = os.path.dirname(self.input_path)
        if source_dir:
            dialog_options["initialdir"] = source_dir

        output_path = filedialog.asksaveasfilename(**dialog_options)
        if not output_path:
            return

        fmt = FORMAT_BY_EXT.get(os.path.splitext(output_path)[1].lower())
        if fmt is None:
            messagebox.showerror(
                "Unsupported format",
                "Choose a .gif, .png, .jpg, .webp, or .bmp output file.",
            )
            return
        if self.is_animated and fmt != "GIF":
            messagebox.showerror(
                "Unsupported format", "Animated files can only be exported as GIF."
            )
            return

        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            written = export_cropped(
                self.input_path, output_path, self.crop, output_size, fmt
            )
        except Exception as exc:
            messagebox.showerror("Export failed", f"Could not export:\n{exc}")
            return
        finally:
            self.root.configure(cursor="")

        frame_word = "frame" if written == 1 else "frames"
        messagebox.showinfo(
            "Export complete",
            f"Saved {written} {frame_word} at {output_size[0]} x {output_size[1]} px "
            f"to:\n{output_path}",
        )


def main():
    root = tk.Tk()
    CropResizeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
