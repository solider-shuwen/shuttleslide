(function() {
    'use strict';

    let currentSlide = 0;
    const slides = document.querySelectorAll('.reveal section');
    const totalSlides = slides.length;

    // Create UI elements
    function createControls() {
        const controls = document.createElement('div');
        controls.className = 'controls';

        const prevBtn = document.createElement('button');
        prevBtn.innerHTML = '&#10094;'; // Left arrow
        prevBtn.title = 'Previous slide';
        prevBtn.addEventListener('click', prevSlide);

        const nextBtn = document.createElement('button');
        nextBtn.innerHTML = '&#10095;'; // Right arrow
        nextBtn.title = 'Next slide';
        nextBtn.addEventListener('click', nextSlide);

        controls.appendChild(prevBtn);
        controls.appendChild(nextBtn);
        document.querySelector('.reveal').appendChild(controls);

        return { prevBtn, nextBtn };
    }

    function createProgress() {
        const progress = document.createElement('div');
        progress.className = 'progress';

        const progressBar = document.createElement('div');
        progressBar.className = 'progress-bar';
        progressBar.style.width = '0%';

        progress.appendChild(progressBar);
        document.querySelector('.reveal').appendChild(progress);

        return progressBar;
    }

    function createSlideNumber() {
        const slideNum = document.createElement('div');
        slideNum.className = 'slide-number';
        slideNum.textContent = '1 / ' + totalSlides;
        document.querySelector('.reveal').appendChild(slideNum);

        return slideNum;
    }

    function showSlide(index, direction) {
        // Wrap around
        if (index >= totalSlides) index = 0;
        if (index < 0) index = totalSlides - 1;

        const directionClass = direction > 0 ? 'future' : 'past';

        // Update slide classes
        slides.forEach((slide, i) => {
            slide.classList.remove('past', 'present', 'future');

            if (i < index) {
                slide.classList.add('past');
            } else if (i === index) {
                slide.classList.add('present');
            } else {
                slide.classList.add('future');
            }
        });

        currentSlide = index;

        // Update UI
        updateUI();

        // Reset and trigger animations
        if (window.SlideAnimations) {
            window.SlideAnimations.reset(currentSlide);
        }
    }

    function nextSlide() {
        showSlide(currentSlide + 1, 1);
    }

    function prevSlide() {
        showSlide(currentSlide - 1, -1);
    }

    function updateUI() {
        // Update progress bar
        const progressBar = document.querySelector('.progress-bar');
        if (progressBar) {
            const progress = ((currentSlide + 1) / totalSlides) * 100;
            progressBar.style.width = progress + '%';
        }

        // Update slide number
        const slideNum = document.querySelector('.slide-number');
        if (slideNum) {
            slideNum.textContent = (currentSlide + 1) + ' / ' + totalSlides;
        }

        // Update button states
        const prevBtn = document.querySelector('.controls button:first-child');
        const nextBtn = document.querySelector('.controls button:last-child');
        if (prevBtn) prevBtn.disabled = currentSlide === 0;
        if (nextBtn) nextBtn.disabled = currentSlide === totalSlides - 1;
    }

    function handleKeyPress(e) {
        switch(e.key) {
            case 'ArrowRight':
            case ' ': // Space
            case 'Enter':
            case 'PageDown':
                e.preventDefault();
                nextSlide();
                break;
            case 'ArrowLeft':
            case 'PageUp':
                e.preventDefault();
                prevSlide();
                break;
            case 'Home':
                e.preventDefault();
                showSlide(0, 1);
                break;
            case 'End':
                e.preventDefault();
                showSlide(totalSlides - 1, -1);
                break;
            case 'f':
            case 'F':
                // Toggle fullscreen
                if (document.documentElement.requestFullscreen) {
                    if (!document.fullscreenElement) {
                        document.documentElement.requestFullscreen();
                    } else {
                        document.exitFullscreen();
                    }
                }
                break;
        }
    }

    // Touch support
    let touchStartX = 0;
    let touchEndX = 0;

    function handleTouchStart(e) {
        touchStartX = e.changedTouches[0].screenX;
    }

    function handleTouchEnd(e) {
        touchEndX = e.changedTouches[0].screenX;
        handleSwipe();
    }

    function handleSwipe() {
        const swipeThreshold = 50;
        const diff = touchStartX - touchEndX;

        if (Math.abs(diff) > swipeThreshold) {
            if (diff > 0) {
                nextSlide();
            } else {
                prevSlide();
            }
        }
    }

    // Initialize
    function init() {
        if (slides.length === 0) return;

        // Create UI elements
        const { prevBtn, nextBtn } = createControls();
        createProgress();
        createSlideNumber();

        // Show first slide
        showSlide(0, 1);

        // Event listeners
        document.addEventListener('keydown', handleKeyPress);
        document.addEventListener('touchstart', handleTouchStart, false);
        document.addEventListener('touchend', handleTouchEnd, false);

        // Mouse wheel support
        let wheelTimeout;
        document.addEventListener('wheel', function(e) {
            clearTimeout(wheelTimeout);
            wheelTimeout = setTimeout(function() {
                if (e.deltaY > 0) {
                    nextSlide();
                } else if (e.deltaY < 0) {
                    prevSlide();
                }
            }, 50);
        }, { passive: true });
    }

    // Start when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();