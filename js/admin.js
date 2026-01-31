fetch("http://127.0.0.1:5000/meetings")
    .then(response => response.json())
    .then(data => {
        const container = document.querySelector(".meetings");
        container.innerHTML = "";

        data.forEach(meeting => {
            const card = document.createElement("div");
            card.className = "meeting-card";

            card.innerHTML = `
                <h3>${meeting.title}</h3>
                <p>Date: ${meeting.datetime}</p>
                <p>Status: <span class="status draft">Draft</span></p>
                <button class="btn small">Open</button>
            `;

            container.appendChild(card);
        });
    });
