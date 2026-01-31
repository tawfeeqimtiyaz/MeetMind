async function uploadRecording() {
    const fileInput = document.getElementById("recordingFile");
    const transcriptBox = document.getElementById("transcriptText");

    if (!fileInput.files.length) {
        alert("Please select a file");
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    const meetingId = 1; // TEMP: later dynamic

    const response = await fetch(
        `http://127.0.0.1:5000/upload-recording/${meetingId}`,
        {
            method: "POST",
            body: formData
        }
    );

    const data = await response.json();
    transcriptBox.innerText = data.transcript;
}
