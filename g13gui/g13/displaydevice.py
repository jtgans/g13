import PIL
import struct
from io import BytesIO

from g13gui.bitwidgets.displaydevice import DisplayDevice


class DisplayMetrics(object):
    WIDTH_PIXELS = 160
    HEIGHT_PIXELS = 48


def ImageToLPBM(image):
    #Simple function to convert a PIL Image into LPBM format.
    
    # Ensure the image is in grayscale ('L') or monochrome ('1') mode
    image = image.convert('1')  # Convert to monochrome (1-bit) if not already
    pixels = image.load()  # Access the pixel data directly
    bio = BytesIO()
    
    # Assuming DisplayMetrics with fixed width and height
    maxBytes = (DisplayMetrics.WIDTH_PIXELS * DisplayMetrics.HEIGHT_PIXELS) // 8
    
    row = 0
    col = 0

    for byteNum in range(0, maxBytes):
        b = 0

        # Adjust the number of rows processed in a byte based on row position
        maxSubrow = 3 if row == 40 else 8

        for subrow in range(maxSubrow):
            # Get pixel value at (col, row + subrow) and shift into position
            pixel_value = pixels[col, row + subrow]
            b |= (1 if pixel_value == 0 else 0) << subrow  # 0 = black, 1 = white in '1' mode

        # Write the byte to the output buffer
        bio.write(struct.pack('<B', b))

        # Update column and row positions
        col += 1
        if col % DisplayMetrics.WIDTH_PIXELS == 0:
            col = 0
            row += 8

    return bio.getvalue()


class G13DisplayDevice(DisplayDevice):
    """A bitwidget display device for the G13 LCD"""

    def __init__(self, manager):
        self._manager = manager

    @property
    def dimensions(self):
        return (DisplayMetrics.WIDTH_PIXELS, DisplayMetrics.HEIGHT_PIXELS)

    def update(self, image):
        lpbm = ImageToLPBM(image)
        self._pushFrame(lpbm)

    def _pushFrame(self, lpbm):
        self._manager.setLCDBuffer(lpbm)
