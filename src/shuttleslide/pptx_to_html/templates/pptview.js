var PPTView = (function() {
    'use strict';

    var totalSlides = {{ slides|length }};
    var slideWidth = {{ slide_width }};
    var slideHeight = {{ slide_height }};

    // Editor state
    var currentIndex = 0;

    // Play state
    var playIndex = 0;
    var isPlaying = false;

    // DOM references (initialized on load)
    var thumbnails, slideFulls, playSlides;
    var slideCounter, playCounter;
    var overlay;

    function init() {
        thumbnails = document.querySelectorAll('.thumbnail');
        slideFulls = document.querySelectorAll('.slide-full');
        playSlides = document.querySelectorAll('.play-slide');
        slideCounter = document.querySelector('.slide-counter');
        playCounter = document.getElementById('playCounter');
        overlay = document.getElementById('playOverlay');

        // Scale thumbnails to fit sidebar width
        scaleThumbnails();

        // Scale main panel slide
        scaleMainSlide();

        // Listen for resize
        window.addEventListener('resize', function() {
            scaleThumbnails();
            scaleMainSlide();
            if (isPlaying) scalePlaySlides();
        });

        // Keyboard shortcuts
        document.addEventListener('keydown', handleKey);

        // Click on play overlay to advance
        if (overlay) {
            overlay.addEventListener('click', function(e) {
                if (e.target.closest('.play-controls')) return;
                nextPlay();
            });
        }
    }

    function scaleThumbnails() {
        var sidebarWidth = 180; // sidebar inner width approx
        var thumbScale = sidebarWidth / slideWidth;
        var thumbHeight = slideHeight * thumbScale;

        var contents = document.querySelectorAll('.thumbnail-content');
        contents.forEach(function(el) {
            el.style.transform = 'scale(' + thumbScale + ')';
            el.style.transformOrigin = 'top left';
        });

        // Set thumbnail wrapper to scaled size
        thumbnails.forEach(function(el) {
            el.style.width = sidebarWidth + 'px';
            el.style.height = thumbHeight + 'px';
        });
    }

    function scaleMainSlide() {
        var viewer = document.querySelector('.slide-viewer');
        if (!viewer) return;

        var vw = viewer.clientWidth - 40; // padding
        var vh = viewer.clientHeight - 40;
        var scale = Math.min(vw / slideWidth, vh / slideHeight, 1);

        var container = document.querySelector('.slide-full.active .slide-container');
        if (container) {
            container.style.transform = 'scale(' + scale + ')';
            container.style.transformOrigin = 'center center';
        }
    }

    function scalePlaySlides() {
        var container = document.querySelector('.play-overlay');
        if (!container) return;

        var vw = window.innerWidth;
        var vh = window.innerHeight - 60; // controls height
        var scale = Math.min(vw / slideWidth, vh / slideHeight);

        // Pre-scale ALL play slides so switching is instant
        var containers = document.querySelectorAll('.play-slide .slide-container');
        containers.forEach(function(el) {
            el.style.transform = 'scale(' + scale + ')';
            el.style.transformOrigin = 'center center';
        });
    }

    // ===== Editor Navigation =====

    function goTo(index) {
        if (index < 0 || index >= totalSlides) return;

        // Update thumbnails
        thumbnails.forEach(function(t, i) {
            t.classList.toggle('active', i === index);
        });

        // Update main panel
        slideFulls.forEach(function(s, i) {
            s.classList.toggle('active', i === index);
        });

        currentIndex = index;
        if (slideCounter) {
            slideCounter.textContent = (index + 1) + ' / ' + totalSlides;
        }

        // Re-scale after switching
        setTimeout(scaleMainSlide, 0);

        // Scroll thumbnail into view
        if (thumbnails[index]) {
            thumbnails[index].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    // ===== Play Mode =====

    function play() {
        playIndex = currentIndex;
        isPlaying = true;

        // Pre-scale all play slides BEFORE showing overlay
        scalePlaySlides();

        if (overlay) {
            overlay.classList.add('active');
        }

        showPlaySlide(playIndex);

        // Try fullscreen
        if (document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(function() {});
        }
    }

    function exitPlay() {
        isPlaying = false;

        if (overlay) {
            overlay.classList.remove('active');
        }

        if (document.exitFullscreen && document.fullscreenElement) {
            document.exitFullscreen().catch(function() {});
        }

        // Sync editor to last play position
        goTo(playIndex);
    }

    function showPlaySlide(index) {
        playSlides.forEach(function(s, i) {
            s.classList.toggle('present', i === index);
        });
        playIndex = index;

        if (playCounter) {
            playCounter.textContent = (index + 1) + ' / ' + totalSlides;
        }
    }

    function nextPlay() {
        if (playIndex < totalSlides - 1) {
            showPlaySlide(playIndex + 1);
        } else {
            // Last slide: exit play
            exitPlay();
        }
    }

    function prevPlay() {
        if (playIndex > 0) {
            showPlaySlide(playIndex - 1);
        }
    }

    // ===== Keyboard =====

    function handleKey(e) {
        if (isPlaying) {
            switch (e.key) {
                case 'Escape':
                    e.preventDefault();
                    exitPlay();
                    break;
                case 'ArrowRight':
                case ' ':
                case 'Enter':
                case 'PageDown':
                    e.preventDefault();
                    nextPlay();
                    break;
                case 'ArrowLeft':
                case 'PageUp':
                    e.preventDefault();
                    prevPlay();
                    break;
                case 'Home':
                    e.preventDefault();
                    showPlaySlide(0);
                    break;
                case 'End':
                    e.preventDefault();
                    showPlaySlide(totalSlides - 1);
                    break;
            }
        } else {
            switch (e.key) {
                case 'ArrowDown':
                case 'PageDown':
                    e.preventDefault();
                    goTo(currentIndex + 1);
                    break;
                case 'ArrowUp':
                case 'PageUp':
                    e.preventDefault();
                    goTo(currentIndex - 1);
                    break;
                case 'Home':
                    e.preventDefault();
                    goTo(0);
                    break;
                case 'End':
                    e.preventDefault();
                    goTo(totalSlides - 1);
                    break;
                case 'F5':
                    e.preventDefault();
                    play();
                    break;
            }
        }
    }

    // Init on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return {
        goTo: goTo,
        play: play,
        exitPlay: exitPlay,
        nextPlay: nextPlay,
        prevPlay: prevPlay
    };
})();
