// Message handling and Excel data insertion are in the inline script in taskpane.html
// This file is kept as a webpack entry point but delegates to the inline handlers

window.onerror = function(message, source, lineno, colno, error) {
    console.error('Global error:', message, 'at', source, lineno, colno);
    return true;
};
