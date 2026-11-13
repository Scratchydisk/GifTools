# GifTools

GifTools is a suite of lightweight tools for editing GIFs. Whether you need to convert an MP4 video into a GIF, crop a square avatar, resize an existing GIF, or shrink a large GIF, GifTools has you covered!

## Features

- **Convert MP4 to GIF:** Transform your MP4 videos into high-quality GIFs quickly and easily.
- **Resize GIF:** Adjust the dimensions of your GIFs to fit your needs.
- **Compress GIF:** Reduce the file size of animated or still GIFs with control over colours, dithering, frame rate, and scale. Set a target file size and the tool will find the largest scale that fits.
- **Crop & Resize:** Visually crop with a locked square selection and export at 80x80, 240x240, or a custom size.
- **Edit Gif Frames:** Add frames or r emove Frames from any GIFs or Create GIFs from single images
- **Add Text:** Add text to spice up that meme!

### Requirements

- ffmpeg
- pillow

## Tools

### Convert MP4 to GIF

![Convert MP4 to GIF](https://github.com/user-attachments/assets/71a51a89-cfcd-4efa-8d33-a7b8de91bd1d)

Download the executable:  
[ConvertMP4toGIF.exe](https://github.com/Kavex/GifTools/releases/download/ConvertMP4toGIF/ConvertMP4toGIF.exe)

Requires: ffmpeg

---

### Resize GIF

![Resize GIF](https://github.com/user-attachments/assets/a771da36-dc46-4274-aaaf-ae6fad65af01)

Download the executable:  
[ResizeGif.exe](https://github.com/Kavex/GifTools/releases/download/ResizeGif/ResizeGif.exe)

Requires: ffmpeg

---

### Compress GIF

Reduce the size of an animated or still GIF. Choose the maximum number of
colours, dithering, frame rate, and scale, or set a target file size and let
the tool find the largest scale that fits. Source frame timing and loop count
are preserved unless you choose to resample the frame rate.

Run:

```bash
python CompressGif.py
```

Requires: ffmpeg, pillow  
Optional: gifsicle (enables lossy optimisation; `sudo apt install gifsicle`)

---

### Crop & Resize GIF

Visually crop a GIF or still image with a fixed 1:1 selection, then export at
80x80, 240x240, or a custom width and height. The crop box can be dragged,
resized from any corner, or nudged with the arrow keys.

Run:

```bash
python CropResizeGif.py
```

Requires: pillow

---

### Edit GIF Frames

![image](https://github.com/user-attachments/assets/a8c77480-47db-46b0-a73b-ca7594a8fff8)

Download the executable:  
[EditGifFrames.exe](https://github.com/Kavex/GifTools/releases/download/EditGifFrames/EditGifFrames.exe)

Requires: ffmpeg

---

### Add Text to GIF

![AddTextToGif](https://github.com/user-attachments/assets/1ed5e095-e5fd-40cc-b78a-244d826ad9d5)

Download the executable:
[AddTextToGIF.exe](https://github.com/Kavex/GifTools/releases/download/AddTextToGif/AddTextToGif.exe)

Requires: pillow

---


## How to Use

1. **Download** the executable for the desired tool from the links above.
2. **Run** the executable on your machine.
3. **Follow** the on-screen options to complete your GIF editing task.

## Contributing

Contributions are welcome! If you'd like to improve GifTools

## License

Distributed under the GNU GENERAL PUBLIC LICENSE License. See the [LICENSE](LICENSE) file for details.
