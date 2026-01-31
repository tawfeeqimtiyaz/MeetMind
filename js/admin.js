// Admin JS — kept minimal and safe 🔧
console.log("Admin dashboard loaded");

document.addEventListener('DOMContentLoaded', () => {
    const btn = document.querySelector('.primary');
    if (!btn) return; // defensive: avoid errors if markup changes

    btn.addEventListener('click', () => {
        alert('Create Meeting form will open here');
    });
});
