# ตั้งค่า Google Drive สำหรับ Runner

1. เปิด Google Cloud Console เลือกหรือสร้างโปรเจกต์ แล้วเปิด **Google Drive API** ใน APIs & Services
2. ใน **Google Auth Platform** ตั้งค่า Branding และ Audience สำหรับบัญชี Gmail ส่วนตัวเลือก External ถ้าแอปอยู่ในสถานะ Testing ให้เพิ่มอีเมลของคุณใน Test users ใน Data Access เพิ่มขอบเขต `https://www.googleapis.com/auth/drive` แอปใช้สิทธิ์นี้เพื่อเปิดลิงก์ไฟล์เดิมและเขียนลงโฟลเดอร์ปลายทางสองแห่ง
3. เลือก **Clients → Create client → Desktop app** ดาวน์โหลด JSON ตั้งชื่อเป็น `client_secret.json` แล้ววางที่ `%LOCALAPPDATA%\ManhwaOCREditor\client_secret.json`
4. ดับเบิลคลิก `SETUP_GDRIVE_AUTH.bat` ด้วยบัญชี Windows ที่เปิด Runner เลือกบัญชี Google ที่อ่านต้นทางและเขียนในโฟลเดอร์ปลายทางได้ เมื่อยืนยันแล้ว โปรแกรมจะเก็บสิทธิ์ไว้ใน `%LOCALAPPDATA%\ManhwaOCREditor\gdrive_token.json` หาก Google เพิกถอนสิทธิ์หรือ refresh token หมดอายุ ให้รันไฟล์ BAT อีกครั้ง

เครื่องนี้ติดตั้งไลบรารีแล้ว สำหรับเครื่องใหม่ให้รัน `.venv\Scripts\python.exe -m pip install -r requirements_gdrive.txt` จากโฟลเดอร์โปรแกรม [คู่มือตั้งค่าจาก Google](https://developers.google.com/workspace/drive/api/quickstart/python)

## สั่งงาน

เปิด GitHub Actions → Manhwa OCR to Photoshop → Run workflow บนกิ่ง main หรือใช้ CLI บนเครื่อง

- **MTP** เลือก `mtp` วางลิงก์ไฟล์ โปรแกรมใช้ SmartStitch 720 × 15000 และโปรไฟล์การ์ตูนต่อสู้เป็นค่าเริ่มต้น
- **ส่งแปล** เลือก `image_folder` วางลิงก์โฟลเดอร์ภาพ เว้นช่องคำแปล โปรแกรมส่ง TXT แบบมีรหัสและแบบเลขข้อทันทีหลัง OCR จากนั้นสร้างมาสก์ คลีน และส่ง PSD โปรแกรมพิมพ์ลิงก์โฟลเดอร์ผลลัพธ์ใน Log
- **รับคำแปล** เลือก `image_folder` ใช้ลิงก์โฟลเดอร์เดิมและเพิ่มลิงก์ไฟล์ TXT เลือกโปรไฟล์และ SmartStitch ได้ ระบบใช้ภาพและแผ่นคลีนจากรอบแรก ควรเก็บทั้ง runner_jobs และ runner_cache ใน LocalAppData จนจบงาน และใช้ TXT จากรอบ OCR ล่าสุดของตอนนั้น

```powershell
.\.venv\Scripts\python.exe -X utf8 cli_pipeline.py --input-mode image_folder --drive-link "<ลิงก์โฟลเดอร์ภาพ>" --lang en
.\.venv\Scripts\python.exe -X utf8 cli_pipeline.py --input-mode image_folder --drive-link "<ลิงก์โฟลเดอร์เดิม>" --translation-link "<ลิงก์ TXT>"
.\.venv\Scripts\python.exe -X utf8 cli_pipeline.py --input-mode mtp --drive-link "<ลิงก์ไฟล์ MTP>"
```
