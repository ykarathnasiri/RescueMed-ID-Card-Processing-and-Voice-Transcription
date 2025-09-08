from fastapi import FastAPI, File, UploadFile, HTTPException
from google.cloud import documentai
from google.api_core.client_options import ClientOptions
from datetime import datetime
import re
import uvicorn
import google.generativeai as genai
import tempfile
import os

app = FastAPI(title="ID Card Document AI Processor")

# Configure Gemini API
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"
genai.configure(api_key=GEMINI_API_KEY)

def process_document_sample(
    project_id: str,
    location: str,
    processor_id: str,
    file_content: bytes,
    mime_type: str,
    processor_version: str = "pretrained-foundation-model-v1.5-pro-2025-06-20"
):
    """Process document using Google Document AI"""
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=opts)
    
    # Use the specific processor version
    name = client.processor_version_path(
        project_id, location, processor_id, processor_version
    )
    
    raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)
    
    request = documentai.ProcessRequest(
        name=name,
        raw_document=raw_document,
    )
    
    result = client.process_document(request=request)
    return result.document

def calculate_age_from_dob(dob_str):
    """Calculate age from date of birth string"""
    if not dob_str:
        return None
    
    try:
        # Try different date formats
        date_formats = [
            "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", 
            "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y",
            "%Y%m%d", "%d %m %Y", "%m %d %Y"
        ]
        
        dob = None
        for fmt in date_formats:
            try:
                dob = datetime.strptime(dob_str.strip(), fmt)
                break
            except:
                continue
        
        if dob:
            today = datetime.now()
            age = today.year - dob.year
            if today.month < dob.month or (today.month == dob.month and today.day < dob.day):
                age -= 1
            return age
    except:
        pass
    
    return None

def extract_gender_from_nic(nic):
    """Extract gender from Sri Lankan NIC number"""
    if not nic:
        return "Unknown"
    
    # Remove any non-alphanumeric characters and convert to uppercase
    nic = re.sub(r'[^0-9A-Za-z]', '', nic.upper())
    
    try:
        # Old NIC format (9 digits + letter V/X)
        if len(nic) == 10 and nic[-1] in ['V', 'X']:
            # Extract day-of-year count (digits 3-5, positions 2-4 in 0-indexed)
            day_code = int(nic[2:5])
        
        # New NIC format (12 digits)
        elif len(nic) == 12 and nic.isdigit():
            # Extract day-of-year count (digits 5-7, positions 4-6 in 0-indexed)
            day_code = int(nic[4:7])
        
        else:
            return "Unknown"
        
        # Determine gender based on day code
        if day_code < 500:
            return "Male"
        elif day_code >= 501:  # 501-866 for females
            return "Female"
        else:
            return "Unknown"
            
    except (ValueError, IndexError):
        return "Unknown"

def extract_labels(document):
    """Extract specific labels from the document"""
    labels = {
        "id": "",
        "name": "",
        "address": "",
        "dob": "",
        "bg": "",
        "age": None,
        "gender": "Unknown"
    }
    
    # Extract from entities if available
    if hasattr(document, 'entities') and document.entities:
        for entity in document.entities:
            entity_type = entity.type_.lower()
            entity_text = entity.mention_text.strip()
            
            # Map entity types to our labels
            if any(keyword in entity_type for keyword in ['id', 'nic', 'number', 'identity']):
                labels["id"] = entity_text
            elif any(keyword in entity_type for keyword in ['name', 'full_name']):
                labels["name"] = entity_text
            elif any(keyword in entity_type for keyword in ['address', 'residence']):
                labels["address"] = entity_text
            elif any(keyword in entity_type for keyword in ['dob', 'birth', 'date_of_birth']):
                labels["dob"] = entity_text
            elif any(keyword in entity_type for keyword in ['bg', 'blood', 'blood_group']):
                labels["bg"] = entity_text
    
    # If no entities found, try to extract from form fields
    if hasattr(document, 'pages') and document.pages:
        for page in document.pages:
            if hasattr(page, 'form_fields'):
                for field in page.form_fields:
                    if hasattr(field, 'field_name') and hasattr(field, 'field_value'):
                        field_name = field.field_name.text_anchor.content.lower() if field.field_name.text_anchor else ""
                        field_value = field.field_value.text_anchor.content.strip() if field.field_value.text_anchor else ""
                        
                        if any(keyword in field_name for keyword in ['id', 'nic']):
                            labels["id"] = field_value
                        elif any(keyword in field_name for keyword in ['name']):
                            labels["name"] = field_value
                        elif any(keyword in field_name for keyword in ['address']):
                            labels["address"] = field_value
                        elif any(keyword in field_name for keyword in ['dob', 'birth']):
                            labels["dob"] = field_value
                        elif any(keyword in field_name for keyword in ['bg', 'blood']):
                            labels["bg"] = field_value
    
    # Calculate age from DOB if available
    if labels["dob"]:
        labels["age"] = calculate_age_from_dob(labels["dob"])
    
    # Extract gender from NIC if available
    if labels["id"]:
        labels["gender"] = extract_gender_from_nic(labels["id"])
    
    return labels

async def transcribe_audio_with_gemini(file_content: bytes, file_extension: str):
    """Transcribe audio to Sinhala text using Gemini API"""
    try:
        # Create a temporary file to save the audio
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{file_extension}") as temp_file:
            temp_file.write(file_content)
            temp_file_path = temp_file.name
        
        try:
            # Upload the audio file to Gemini
            audio_file = genai.upload_file(temp_file_path)
            
            # Initialize the Gemini model
            model = genai.GenerativeModel("gemini-1.5-flash")
            
            # Create prompt for Sinhala transcription
            prompt = """
            Please transcribe this audio file to Sinhala text. 
            Return only the Sinhala transcription without any additional text or explanations.
            If the audio is in a different language, please still provide the transcription in Sinhala if possible.
            """
            
            # Generate transcription
            response = model.generate_content([prompt, audio_file])
            
            return response.text.strip()
            
        finally:
            # Clean up the temporary file
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
                
    except Exception as e:
        raise Exception(f"Error transcribing audio: {str(e)}")

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    """
    Transcribe audio file to Sinhala text using Gemini API
    """
    try:
        # Validate file
        if not file.filename:
            raise HTTPException(status_code=400, detail="File name is required")
        
        # Check file extension
        file_extension = file.filename.lower().split('.')[-1]
        supported_audio_formats = ['mp3', 'wav', 'flac', 'm4a', 'aac', 'ogg', 'wma']
        
        if file_extension not in supported_audio_formats:
            raise HTTPException(
                status_code=400, 
                detail=f"Unsupported audio format: {file_extension}. Supported formats: {', '.join(supported_audio_formats)}"
            )
        
        # Read file content
        file_content = await file.read()
        
        # Transcribe using Gemini
        transcription = await transcribe_audio_with_gemini(file_content, file_extension)
        
        return {
            "transcription": transcription,
            "language": "sinhala",
            "filename": file.filename
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing audio: {str(e)}")

@app.post("/process")
async def process_id_card(file: UploadFile = File(...)):
    """
    Process ID card image and extract labeled information
    """
    try:
        # Read file content
        file_content = await file.read()
        
        # Determine MIME type from file extension
        if not file.filename:
            raise HTTPException(status_code=400, detail="File name is required")
        
        file_extension = file.filename.lower().split('.')[-1]
        mime_type_mapping = {
            'pdf': 'application/pdf',
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'gif': 'image/gif',
            'tiff': 'image/tiff',
            'tif': 'image/tiff',
            'bmp': 'image/bmp',
            'webp': 'image/webp'
        }
        
        mime_type = mime_type_mapping.get(file_extension)
        if not mime_type:
            raise HTTPException(
                status_code=400, 
                detail=f"Unsupported file type: {file_extension}"
            )
        
        # Process document with Document AI
        document = process_document_sample(
            project_id="505110297716",
            location="us",
            processor_id="4df6e1de8a6be84b",
            file_content=file_content,
            mime_type=mime_type
        )
        
        # Extract labels
        labels = extract_labels(document)
        
        return labels
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing document: {str(e)}")

@app.get("/")
async def health_check():
    """Health check endpoint"""
    return {"status": "OK", "message": "ID Card Document AI Processor is running"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

