// 生成应用图标 PNG（256x256，深青底 + 白色 N 形）
const fs = require('node:fs');
const zlib = require('node:zlib');
const path = require('node:path');

const SIZE = 256;
const OUT = path.join(__dirname, '..', 'assets', 'icon.png');

// CRC32
const crcTable = [];
for (let n = 0; n < 256; n++) {
  let c = n;
  for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
  crcTable[n] = c >>> 0;
}
function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i++) c = crcTable[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, 'ascii');
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([len, typeBuf, data, crc]);
}

// 构建像素数据
const raw = Buffer.alloc(SIZE * (1 + SIZE * 3));
let offset = 0;
for (let y = 0; y < SIZE; y++) {
  raw[offset++] = 0; // filter: none
  for (let x = 0; x < SIZE; x++) {
    // 渐变背景：深青 #0f3d3e → 深蓝 #1a2744
    const t = y / SIZE;
    let r = Math.round(15 + (26 - 15) * t);
    let g = Math.round(61 + (39 - 61) * t);
    let b = Math.round(62 + (68 - 62) * t);

    // 白色 N 形（粗体）
    const nx = 70, nw = 116, ny = 70, nh = 116, stroke = 22;
    const inLeft = x >= nx && x < nx + stroke && y >= ny && y < ny + nh;
    const inRight = x >= nx + nw - stroke && x < nx + nw && y >= ny && y < ny + nh;
    // 对角线
    const diagX = nx + stroke + (x - nx - stroke);
    const onDiag = x >= nx + stroke && x < nx + nw - stroke &&
      y >= ny + (x - nx - stroke) * (nh / (nw - 2 * stroke)) - stroke / 2 &&
      y < ny + (x - nx - stroke) * (nh / (nw - 2 * stroke)) + stroke / 2;
    if (inLeft || inRight || onDiag) { r = 255; g = 255; b = 255; }

    raw[offset++] = r;
    raw[offset++] = g;
    raw[offset++] = b;
  }
}

const ihdr = Buffer.alloc(13);
ihdr.writeUInt32BE(SIZE, 0);
ihdr.writeUInt32BE(SIZE, 4);
ihdr[8] = 8;  // bit depth
ihdr[9] = 2;  // color type RGB
ihdr[10] = 0; // compression
ihdr[11] = 0; // filter
ihdr[12] = 0; // interlace

const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const idat = zlib.deflateSync(raw);
const png = Buffer.concat([sig, chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', Buffer.alloc(0))]);

fs.writeFileSync(OUT, png);
console.log('Icon written:', OUT, png.length, 'bytes');
