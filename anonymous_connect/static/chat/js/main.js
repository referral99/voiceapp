/**
 * Global Anonymous Connect Logic
 */

// Handle Registration Modal Display
function closeModal() {
    const modal = document.getElementById('regModal');
    if (modal) {
        modal.style.transition = 'opacity 0.3s ease';
        modal.style.opacity = '0';
        setTimeout(() => {
            modal.style.display = 'none';
        }, 300);
    }
}

// Close modal if user clicks the backdrop
window.onclick = function(event) {
    const modal = document.getElementById('regModal');
    if (event.target == modal) {
        closeModal();
    }
};

// Log info for debugging in the browser console
console.log("Anonymous Connect Scripts Initialized.");

/**
 * WebRTC Signaling Placeholder
 * Note: Actual WebRTC requires exchanging ICE candidates
 * through the chatSocket defined in call.html.
 */
function initializeVoiceStream() {
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        navigator.mediaDevices.getUserMedia({ audio: true })
            .then(stream => {
                console.log("Microphone access granted.");
                // Here you would add tracks to an RTCPeerConnection
            })
            .catch(err => {
                console.error("Microphone access denied: ", err);
                alert("Please enable microphone access for voice calls.");
            });
    }
}