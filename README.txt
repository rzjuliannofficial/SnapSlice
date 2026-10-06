SnapSlice — Precision Image Splitter
====================================

FUNGSI
------
Aplikasi desktop Windows 11 untuk memecah satu gambar kolase menjadi banyak file PNG berdasarkan kotak yang ditentukan user. Aplikasi tidak mengubah gambar asli.

Batasan penting:
- Bukan penghapus background. Piksel crop pada umumnya disalin apa adanya.
- Tidak memakai ML, AI, OpenCV, TensorFlow, PyTorch, scipy, atau framework GUI berat.
- Tidak melakukan auto-labeling atau deteksi/koreksi orientasi otomatis.
- Tidak melakukan filter, resize sumber, teks, atau penyuntingan gambar umum.
- Mode Otomatis hanya memakai Pillow + NumPy.

KEBUTUHAN
---------
- Windows 11 (untuk pengalaman file picker native Windows yang ditargetkan).
- Python 3.9+.
- Pillow >= 10.0.
- NumPy >= 1.23.
- Tkinter/Tcl-Tk tersedia pada instalasi Python.

STRUKTUR
--------
YKAN_Manual_Reviewer/
├── pecah gambar.py
├── requirements.txt
├── JALANKAN.bat
└── README.txt

File runtime dibuat otomatis di samping pecah gambar.py:
- pengaturan.json
- log_error.txt
- log_error.old.txt jika log melewati sekitar 1 MB
- sesi/<hash>.json
- sesi/<hash>.bak1 sampai <hash>.bak5

CARA MENJALANKAN
----------------
Cara utama di Windows:
1. Masuk ke folder YKAN_Manual_Reviewer.
2. Klik dua kali JALANKAN.bat.
3. Script mencari Python dengan py -3 lalu python nyata, mengecek Tkinter dan dependency.
4. Jika dependency belum ada, script menjalankan pip install -r requirements.txt.
5. Aplikasi kemudian dibuka.

Uji tanpa GUI:
    py -3 "pecah gambar.py" --selftest
atau:
    python "pecah gambar.py" --selftest
Hasil sukses harus mencetak LULUS.

INPUT GAMBAR
------------
Format: PNG, JPG/JPEG, WEBP, BMP, TIF/TIFF.

Path dapat dimuat melalui:
- Buka Gambar: file picker native Windows.
- Kolom path + Muat: cocok untuk paste "Copy as path" Windows 11.
Spasi di awal/akhir, tanda kutip pembungkus, spasi dalam path, dan karakter non-ASCII dibersihkan/ditangani. Path relatif dibaca relatif terhadap folder pecah gambar.py.

Folder output default:
    folder_gambar_asli/nama_gambar_pecahan/
Jika folder default sudah ada, nama folder baru memakai akhiran _2, _3, dan seterusnya. Folder yang sengaja dipilih melalui Pilih Folder Output dipakai apa adanya.

MODE MANUAL
-----------
1. Pilih mode Manual.
2. Drag tombol kiri di area kosong canvas untuk membuat kotak.
3. Drag di dalam kotak untuk menggeser.
4. Gunakan 8 handle untuk resize.
5. Kotak dapat dihapus dengan Hapus Kotak/Delete.
6. Kotak hasil Grid dan Otomatis juga dapat diedit dengan cara sama.

MODE GRID
---------
Masukkan:
- Baris >= 1
- Kolom >= 1
- Margin >= 0
- Gap >= 0

Klik Terapkan Grid. Rumus sel memakai posisi kumulatif floating point lalu round, sehingga tidak terjadi akumulasi kesalahan pembulatan. Dengan gap, area gap memang tidak ikut di-crop.

MODE OTOMATIS
-------------
Tidak ada ML/AI/OpenCV. Algoritma:
1. Jika gambar punya alpha dan minimal 5% piksel alpha < 250, mode ALPHA dipakai.
   Foreground = alpha > ambang alpha. RGB diabaikan total.
2. Jika tidak, mode WARNA dipakai.
   Background dipilih dari warna paling sering pada tepi gambar setelah kuantisasi per 8 level.
   Foreground = max(|R-Rbg|, |G-Gbg|, |B-Bbg|) > toleransi.
3. Mask didilasi dengan MaxFilter Pillow menggunakan jarak gabung.
4. Komponen 8-ketetanggaan dilabel dengan RLE + union-find.
5. Bounding box dihitung dari mask asli sebelum dilasi, kemudian padding dan clamp.
6. Kotak kecil dari ukuran minimum dibuang dan jumlahnya ditampilkan.
7. Gabung kotak hanya bila >=90% luas kotak yang lebih kecil berada di dalam kotak lain. Irisan biasa tidak otomatis digabung.
8. Urutan akhir: atas ke bawah, kiri ke kanan menggunakan pusat-y dan median tinggi kotak.

Parameter:
- Toleransi warna: 0-100, default 30.
- Jarak gabung: 0-20, default 2.
- Ukuran minimum: default 16 px.
- Padding: default 2 px.
- Ambang alpha: 1-254, default 128.
- Gabung kotak 90%: aktif.
- Bersihkan piksel tetangga: aktif hanya untuk sumber yang masuk MODE ALPHA.

Peringatan jarak gabung:
Di atas 8 px aplikasi menampilkan peringatan karena objek berdekatan dapat ikut menyatu. Nilai besar secara agresif dapat meruntuhkan banyak objek menjadi sedikit kotak.

BACKGROUND TIDAK JELAS
---------------------
Jika empat sudut berbeda jauh, aplikasi memberi peringatan. Pada MODE WARNA tersedia tombol Ambil warna background. Klik tombol, lalu klik piksel background pada canvas. Warna tersebut menimpa tebakan otomatis. Jalankan Deteksi Otomatis lagi untuk memakai warna baru.

KOTAK KECIL / SERPIHAN
----------------------
Kotak dengan luas < 10% median luas kotak ditandai [!] di canvas dan list. Kotak tidak dibuang otomatis. User yang memutuskan apakah akan dihapus, digeser, atau diperbesar.

EKSEKUSI & EKSPOR
-----------------
- Tombol "⚡ Eksekusi Pecah Gambar" (Panel Otomatis): jika belum ada kotak akan langsung mendeteksi lalu mengekspor gambar, jika sudah ada kotak langsung memotong & menyimpan ke folder output.
- Tombol "⚡ Eksekusi Pecah Semua (Simpan)" (Bilah Bawah): memotong semua kotak yang ada di listbox dan menyimpan ke folder output.
- Tombol "Eksekusi Pecah Terpilih": hanya memotong kotak yang sedang dipilih di listbox.
- Output selalu PNG tanpa resize/rotasi/perubahan warna.
- Mode warna/alpha sumber dipertahankan selama format PNG mendukung mode tersebut.
- Jika file tujuan sudah ada, aplikasi tidak menimpa. Contoh: ikon_001.png menjadi ikon_001_2.png, lalu _3, dst.
- Untuk box otomatis pada gambar alpha, jika Bersihkan piksel tetangga aktif dan box belum diubah user, alpha pixel yang bukan komponen box tersebut diubah menjadi 0. Piksel objeknya sendiri tidak diubah.
- Box yang sudah digeser/resized user, serta box Grid/Manual, tidak dibersihkan.

URUTAN BACA
-----------
Kotak diurutkan dengan pusat-y. Sebuah kotak masuk baris yang sama bila selisih pusat-y dengan rata-rata pusat-y baris tersebut < 0.5 x median tinggi kotak. Di dalam baris, urut berdasarkan x1.

SHORTCUT
--------
Ctrl+O       Buka Gambar
Ctrl+E       Ekspor Semua
Delete       Hapus kotak terpilih
Ctrl+A       Pilih semua
Esc          Batal pilih / batal aksi eyedropper
Ctrl+Z       Undo
Ctrl+Y       Redo
+ / -        Zoom in/out
0            Fit-to-window
1            Zoom 100%
Panah        Geser kotak 1 px
Shift+Panah  Geser kotak 10 px
F1           Bantuan singkat

MOUSE / CANVAS
--------------
- Drag kiri area kosong: buat kotak baru.
- Drag kiri dalam kotak: geser.
- Drag handle: resize.
- Roda: scroll vertikal.
- Shift+roda: scroll horizontal.
- Ctrl+roda: zoom dengan kursor sebagai pusat.
- Tombol tengah drag: pan.
- Space+drag: pan.

KOORDINAT DAN PRESISI
---------------------
Kotak disimpan dalam koordinat piksel gambar asli: (x1, y1, x2, y2), bukan koordinat layar. Canvas memakai fungsi konversi dua arah yang memperhitungkan zoom dan pan. Crop memakai Image.crop((x1,y1,x2,y2)), sehingga x2 dan y2 bersifat eksklusif seperti konvensi Pillow.

KEAMANAN DATA / SESI
--------------------
Saat daftar kotak berubah, sesi disimpan setelah debounce sekitar 1 detik. Sebelum ekspor sesi juga disimpan. Sesi memakai hash dari path absolut + ukuran file + mtime file. Penyimpanan JSON dilakukan melalui file sementara lalu replace secara atomic. Lima salinan backup diputar sebagai .bak1 sampai .bak5.

Saat gambar dibuka lagi dan sesi cocok, aplikasi menanyakan:
    "Lanjutkan sesi sebelumnya?"
Jawab Ya untuk memulihkan kotak dan parameter. Jika JSON rusak/tidak cocok, sesi diabaikan dengan peringatan dan aplikasi tetap berjalan.

GAMBAR SANGAT BESAR
-------------------
Jika gambar lebih dari 50.000.000 piksel, aplikasi meminta persetujuan sebelum lanjut. DecompressionBombError dan MemoryError ditangani sebagai error yang dapat dipulihkan. Gunakan gambar lebih kecil jika RAM tidak cukup.

LOG ERROR
---------
Semua exception penting ditulis ke:
    log_error.txt
Format mencakup timestamp, konteks, jenis error, dan traceback lengkap. Saat ukuran sekitar 1 MB tercapai, log diputar menjadi log_error.old.txt.

MASALAH UMUM
------------
Python tidak ditemukan:
Pasang Python 3.9-3.13 dari https://www.python.org/downloads/ . Saat instalasi centang Add python.exe to PATH dan Tcl/Tk. Tutup/buka CMD lalu jalankan JALANKAN.bat lagi.

Tkinter tidak tersedia:
Pasang ulang Python lengkap dengan Tcl/Tk. Cek dengan:
    python -c "import tkinter"

Dependency gagal:
Jalankan:
    python -m pip install -r requirements.txt
Pastikan internet tersedia dan gunakan Python 3.9-3.13.

File picker tidak muncul:
Aplikasi memang menggunakan filedialog.askopenfilename/askdirectory bawaan Tkinter. Pastikan Tkinter terpasang dan jendela aplikasi tidak diblokir oleh security software. Jalur cadangan tetap tersedia: tempel path gambar di kolom lalu klik Muat. Untuk output, masukkan folder lewat dialog; tidak ada file dialog buatan sendiri.

Hasil otomatis terlalu sedikit:
- MODE WARNA: turunkan toleransi bila objek terlalu mirip background.
- Naikkan toleransi bila noise/background ikut dianggap foreground.
- Kurangi jarak gabung bila objek yang berdekatan ikut menyatu.
- Periksa warna background; pakai eyedropper bila empat sudut tidak mewakili background.
- Kurangi padding bila kotak terlalu besar.
- Ukuran minimum yang besar dapat membuang komponen kecil.

Hasil otomatis memecah satu ikon menjadi beberapa kotak:
- Naikkan jarak gabung sedikit.
- Pada gambar alpha, cek ambang alpha. Nilai lebih tinggi membuat lebih sedikit area alpha dianggap foreground.
- Jangan menaikkan jarak gabung berlebihan karena objek tetangga dapat ikut tergabung.

Kotak sudah benar tetapi ekspor berisi objek tetangga pada gambar transparan:
Pastikan box berasal dari Mode Otomatis alpha dan belum digeser/resized. Aktifkan Bersihkan piksel tetangga lalu ekspor ulang. Fitur ini mengubah alpha pixel di dalam crop yang bukan bagian komponen tersebut menjadi 0; RGB tidak diubah.

File hasil sudah ada:
Aplikasi tidak menimpa. File berikutnya otomatis memakai _2, _3, dst.

Path terlalu panjang:
Gunakan folder yang lebih dekat ke root drive dan awalan file yang lebih pendek. Aplikasi sengaja menolak path >260 karakter.

Disk penuh / folder tidak bisa ditulis:
Pilih folder output lain atau kosongkan ruang. Detail error ditulis ke log_error.txt.

GAMBAR ASLI
-----------
Gambar asli dibuka read-only secara logika aplikasi. Aplikasi tidak pernah menyimpan kembali ke path sumber dan tidak melakukan resize/rotasi/perubahan warna pada file sumber. Ekspor selalu diarahkan ke folder output.
