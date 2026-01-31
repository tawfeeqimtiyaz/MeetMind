console.log("Member dashboard loaded");

// Later this will open the meeting card page
document.querySelectorAll(".small").forEach(button => {
    button.addEventListener("click", () => {
        alert("Meeting card page will open here");
    });
});
