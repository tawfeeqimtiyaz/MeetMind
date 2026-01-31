const form = document.querySelector(".meeting-form");

form.addEventListener("submit", function (e) {
    e.preventDefault(); // stop page refresh

    // Collect form data
    const title = form.querySelector('input[type="text"]').value;
    const agenda = form.querySelector('textarea').value;
    const datetime = form.querySelector('input[type="datetime-local"]').value;

    console.log("Meeting Created:");
    console.log("Title:", title);
    console.log("Agenda:", agenda);
    console.log("Date & Time:", datetime);

    alert("Meeting created successfully! (Draft)");

    // Later: send this data to Flask backend
});
