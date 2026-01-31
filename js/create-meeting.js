const form = document.querySelector(".meeting-form");

form.addEventListener("submit", async function (e) {
    e.preventDefault();

    const title = form.querySelector('input[type="text"]').value;
    const agenda = form.querySelectorAll('textarea')[0].value;
    const participants = form.querySelectorAll('textarea')[1].value;
    const datetime = form.querySelector('input[type="datetime-local"]').value;

    const response = await fetch("http://127.0.0.1:5000/create-meeting", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            title,
            agenda,
            datetime,
            participants
        })
    });

    const result = await response.json();
    alert(result.message);

    form.reset();
});
