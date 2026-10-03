document.addEventListener("DOMContentLoaded", function () {
    // Only add the button on the Django/Jazzmin Admin Login page
    if (window.location.pathname === "/admin/login/" || window.location.pathname === "/admin/login") {

        // Prevent duplicate buttons
        if (document.getElementById("back-to-student-portal")) {
            return;
        }

        // Find the login form
        const loginForm = document.querySelector("form");

        if (!loginForm) {
            return;
        }

        // Create the Back link
        const backLink = document.createElement("a");

        backLink.id = "back-to-student-portal";
        backLink.href = "/student/login/";
        backLink.innerHTML = 'Back';

        // Keep it visually consistent with the Jazzmin login page
        backLink.style.display = "block";
        backLink.style.textAlign = "center";
        backLink.style.marginTop = "15px";
        backLink.style.textDecoration = "none";

        // Add it directly below the login form
        loginForm.parentNode.appendChild(backLink);
    }
});