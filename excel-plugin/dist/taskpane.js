/******/ (function() { // webpackBootstrap
/******/ 	// The require scope
/******/ 	var __webpack_require__ = {};
/******/ 	
/************************************************************************/
/******/ 	/* webpack/runtime/make namespace object */
/******/ 	!function() {
/******/ 		// define __esModule on exports
/******/ 		__webpack_require__.r = function(exports) {
/******/ 			if(typeof Symbol !== 'undefined' && Symbol.toStringTag) {
/******/ 				Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });
/******/ 			}
/******/ 			Object.defineProperty(exports, '__esModule', { value: true });
/******/ 		};
/******/ 	}();
/******/ 	
/************************************************************************/
var __webpack_exports__ = {};
// This entry need to be wrapped in an IIFE because it need to be isolated against other entry modules.
!function() {
/*!**********************************!*\
  !*** ./src/taskpane/taskpane.js ***!
  \**********************************/
function _typeof(o) { "@babel/helpers - typeof"; return _typeof = "function" == typeof Symbol && "symbol" == typeof Symbol.iterator ? function (o) { return typeof o; } : function (o) { return o && "function" == typeof Symbol && o.constructor === Symbol && o !== Symbol.prototype ? "symbol" : typeof o; }, _typeof(o); }
function _createForOfIteratorHelper(r, e) { var t = "undefined" != typeof Symbol && r[Symbol.iterator] || r["@@iterator"]; if (!t) { if (Array.isArray(r) || (t = _unsupportedIterableToArray(r)) || e && r && "number" == typeof r.length) { t && (r = t); var _n = 0, F = function F() {}; return { s: F, n: function n() { return _n >= r.length ? { done: !0 } : { done: !1, value: r[_n++] }; }, e: function e(r) { throw r; }, f: F }; } throw new TypeError("Invalid attempt to iterate non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method."); } var o, a = !0, u = !1; return { s: function s() { t = t.call(r); }, n: function n() { var r = t.next(); return a = r.done, r; }, e: function e(r) { u = !0, o = r; }, f: function f() { try { a || null == t.return || t.return(); } finally { if (u) throw o; } } }; }
function _unsupportedIterableToArray(r, a) { if (r) { if ("string" == typeof r) return _arrayLikeToArray(r, a); var t = {}.toString.call(r).slice(8, -1); return "Object" === t && r.constructor && (t = r.constructor.name), "Map" === t || "Set" === t ? Array.from(r) : "Arguments" === t || /^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(t) ? _arrayLikeToArray(r, a) : void 0; } }
function _arrayLikeToArray(r, a) { (null == a || a > r.length) && (a = r.length); for (var e = 0, n = Array(a); e < a; e++) n[e] = r[e]; return n; }
function _regeneratorRuntime() { "use strict"; /*! regenerator-runtime -- Copyright (c) 2014-present, Facebook, Inc. -- license (MIT): https://github.com/facebook/regenerator/blob/main/LICENSE */ _regeneratorRuntime = function _regeneratorRuntime() { return e; }; var t, e = {}, r = Object.prototype, n = r.hasOwnProperty, o = Object.defineProperty || function (t, e, r) { t[e] = r.value; }, i = "function" == typeof Symbol ? Symbol : {}, a = i.iterator || "@@iterator", c = i.asyncIterator || "@@asyncIterator", u = i.toStringTag || "@@toStringTag"; function define(t, e, r) { return Object.defineProperty(t, e, { value: r, enumerable: !0, configurable: !0, writable: !0 }), t[e]; } try { define({}, ""); } catch (t) { define = function define(t, e, r) { return t[e] = r; }; } function wrap(t, e, r, n) { var i = e && e.prototype instanceof Generator ? e : Generator, a = Object.create(i.prototype), c = new Context(n || []); return o(a, "_invoke", { value: makeInvokeMethod(t, r, c) }), a; } function tryCatch(t, e, r) { try { return { type: "normal", arg: t.call(e, r) }; } catch (t) { return { type: "throw", arg: t }; } } e.wrap = wrap; var h = "suspendedStart", l = "suspendedYield", f = "executing", s = "completed", y = {}; function Generator() {} function GeneratorFunction() {} function GeneratorFunctionPrototype() {} var p = {}; define(p, a, function () { return this; }); var d = Object.getPrototypeOf, v = d && d(d(values([]))); v && v !== r && n.call(v, a) && (p = v); var g = GeneratorFunctionPrototype.prototype = Generator.prototype = Object.create(p); function defineIteratorMethods(t) { ["next", "throw", "return"].forEach(function (e) { define(t, e, function (t) { return this._invoke(e, t); }); }); } function AsyncIterator(t, e) { function invoke(r, o, i, a) { var c = tryCatch(t[r], t, o); if ("throw" !== c.type) { var u = c.arg, h = u.value; return h && "object" == _typeof(h) && n.call(h, "__await") ? e.resolve(h.__await).then(function (t) { invoke("next", t, i, a); }, function (t) { invoke("throw", t, i, a); }) : e.resolve(h).then(function (t) { u.value = t, i(u); }, function (t) { return invoke("throw", t, i, a); }); } a(c.arg); } var r; o(this, "_invoke", { value: function value(t, n) { function callInvokeWithMethodAndArg() { return new e(function (e, r) { invoke(t, n, e, r); }); } return r = r ? r.then(callInvokeWithMethodAndArg, callInvokeWithMethodAndArg) : callInvokeWithMethodAndArg(); } }); } function makeInvokeMethod(e, r, n) { var o = h; return function (i, a) { if (o === f) throw Error("Generator is already running"); if (o === s) { if ("throw" === i) throw a; return { value: t, done: !0 }; } for (n.method = i, n.arg = a;;) { var c = n.delegate; if (c) { var u = maybeInvokeDelegate(c, n); if (u) { if (u === y) continue; return u; } } if ("next" === n.method) n.sent = n._sent = n.arg;else if ("throw" === n.method) { if (o === h) throw o = s, n.arg; n.dispatchException(n.arg); } else "return" === n.method && n.abrupt("return", n.arg); o = f; var p = tryCatch(e, r, n); if ("normal" === p.type) { if (o = n.done ? s : l, p.arg === y) continue; return { value: p.arg, done: n.done }; } "throw" === p.type && (o = s, n.method = "throw", n.arg = p.arg); } }; } function maybeInvokeDelegate(e, r) { var n = r.method, o = e.iterator[n]; if (o === t) return r.delegate = null, "throw" === n && e.iterator.return && (r.method = "return", r.arg = t, maybeInvokeDelegate(e, r), "throw" === r.method) || "return" !== n && (r.method = "throw", r.arg = new TypeError("The iterator does not provide a '" + n + "' method")), y; var i = tryCatch(o, e.iterator, r.arg); if ("throw" === i.type) return r.method = "throw", r.arg = i.arg, r.delegate = null, y; var a = i.arg; return a ? a.done ? (r[e.resultName] = a.value, r.next = e.nextLoc, "return" !== r.method && (r.method = "next", r.arg = t), r.delegate = null, y) : a : (r.method = "throw", r.arg = new TypeError("iterator result is not an object"), r.delegate = null, y); } function pushTryEntry(t) { var e = { tryLoc: t[0] }; 1 in t && (e.catchLoc = t[1]), 2 in t && (e.finallyLoc = t[2], e.afterLoc = t[3]), this.tryEntries.push(e); } function resetTryEntry(t) { var e = t.completion || {}; e.type = "normal", delete e.arg, t.completion = e; } function Context(t) { this.tryEntries = [{ tryLoc: "root" }], t.forEach(pushTryEntry, this), this.reset(!0); } function values(e) { if (e || "" === e) { var r = e[a]; if (r) return r.call(e); if ("function" == typeof e.next) return e; if (!isNaN(e.length)) { var o = -1, i = function next() { for (; ++o < e.length;) if (n.call(e, o)) return next.value = e[o], next.done = !1, next; return next.value = t, next.done = !0, next; }; return i.next = i; } } throw new TypeError(_typeof(e) + " is not iterable"); } return GeneratorFunction.prototype = GeneratorFunctionPrototype, o(g, "constructor", { value: GeneratorFunctionPrototype, configurable: !0 }), o(GeneratorFunctionPrototype, "constructor", { value: GeneratorFunction, configurable: !0 }), GeneratorFunction.displayName = define(GeneratorFunctionPrototype, u, "GeneratorFunction"), e.isGeneratorFunction = function (t) { var e = "function" == typeof t && t.constructor; return !!e && (e === GeneratorFunction || "GeneratorFunction" === (e.displayName || e.name)); }, e.mark = function (t) { return Object.setPrototypeOf ? Object.setPrototypeOf(t, GeneratorFunctionPrototype) : (t.__proto__ = GeneratorFunctionPrototype, define(t, u, "GeneratorFunction")), t.prototype = Object.create(g), t; }, e.awrap = function (t) { return { __await: t }; }, defineIteratorMethods(AsyncIterator.prototype), define(AsyncIterator.prototype, c, function () { return this; }), e.AsyncIterator = AsyncIterator, e.async = function (t, r, n, o, i) { void 0 === i && (i = Promise); var a = new AsyncIterator(wrap(t, r, n, o), i); return e.isGeneratorFunction(r) ? a : a.next().then(function (t) { return t.done ? t.value : a.next(); }); }, defineIteratorMethods(g), define(g, u, "Generator"), define(g, a, function () { return this; }), define(g, "toString", function () { return "[object Generator]"; }), e.keys = function (t) { var e = Object(t), r = []; for (var n in e) r.push(n); return r.reverse(), function next() { for (; r.length;) { var t = r.pop(); if (t in e) return next.value = t, next.done = !1, next; } return next.done = !0, next; }; }, e.values = values, Context.prototype = { constructor: Context, reset: function reset(e) { if (this.prev = 0, this.next = 0, this.sent = this._sent = t, this.done = !1, this.delegate = null, this.method = "next", this.arg = t, this.tryEntries.forEach(resetTryEntry), !e) for (var r in this) "t" === r.charAt(0) && n.call(this, r) && !isNaN(+r.slice(1)) && (this[r] = t); }, stop: function stop() { this.done = !0; var t = this.tryEntries[0].completion; if ("throw" === t.type) throw t.arg; return this.rval; }, dispatchException: function dispatchException(e) { if (this.done) throw e; var r = this; function handle(n, o) { return a.type = "throw", a.arg = e, r.next = n, o && (r.method = "next", r.arg = t), !!o; } for (var o = this.tryEntries.length - 1; o >= 0; --o) { var i = this.tryEntries[o], a = i.completion; if ("root" === i.tryLoc) return handle("end"); if (i.tryLoc <= this.prev) { var c = n.call(i, "catchLoc"), u = n.call(i, "finallyLoc"); if (c && u) { if (this.prev < i.catchLoc) return handle(i.catchLoc, !0); if (this.prev < i.finallyLoc) return handle(i.finallyLoc); } else if (c) { if (this.prev < i.catchLoc) return handle(i.catchLoc, !0); } else { if (!u) throw Error("try statement without catch or finally"); if (this.prev < i.finallyLoc) return handle(i.finallyLoc); } } } }, abrupt: function abrupt(t, e) { for (var r = this.tryEntries.length - 1; r >= 0; --r) { var o = this.tryEntries[r]; if (o.tryLoc <= this.prev && n.call(o, "finallyLoc") && this.prev < o.finallyLoc) { var i = o; break; } } i && ("break" === t || "continue" === t) && i.tryLoc <= e && e <= i.finallyLoc && (i = null); var a = i ? i.completion : {}; return a.type = t, a.arg = e, i ? (this.method = "next", this.next = i.finallyLoc, y) : this.complete(a); }, complete: function complete(t, e) { if ("throw" === t.type) throw t.arg; return "break" === t.type || "continue" === t.type ? this.next = t.arg : "return" === t.type ? (this.rval = this.arg = t.arg, this.method = "return", this.next = "end") : "normal" === t.type && e && (this.next = e), y; }, finish: function finish(t) { for (var e = this.tryEntries.length - 1; e >= 0; --e) { var r = this.tryEntries[e]; if (r.finallyLoc === t) return this.complete(r.completion, r.afterLoc), resetTryEntry(r), y; } }, catch: function _catch(t) { for (var e = this.tryEntries.length - 1; e >= 0; --e) { var r = this.tryEntries[e]; if (r.tryLoc === t) { var n = r.completion; if ("throw" === n.type) { var o = n.arg; resetTryEntry(r); } return o; } } throw Error("illegal catch attempt"); }, delegateYield: function delegateYield(e, r, n) { return this.delegate = { iterator: values(e), resultName: r, nextLoc: n }, "next" === this.method && (this.arg = t), y; } }, e; }
function asyncGeneratorStep(n, t, e, r, o, a, c) { try { var i = n[a](c), u = i.value; } catch (n) { return void e(n); } i.done ? t(u) : Promise.resolve(u).then(r, o); }
function _asyncToGenerator(n) { return function () { var t = this, e = arguments; return new Promise(function (r, o) { var a = n.apply(t, e); function _next(n) { asyncGeneratorStep(a, r, o, _next, _throw, "next", n); } function _throw(n) { asyncGeneratorStep(a, r, o, _next, _throw, "throw", n); } _next(void 0); }); }; }
Office.onReady(function (info) {
  if (info.host === Office.HostType.Excel) {
    window.addEventListener('message', handleMessage);
  }
});
function handleMessage(_x) {
  return _handleMessage.apply(this, arguments);
}
function _handleMessage() {
  _handleMessage = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee(event) {
    var parsedData;
    return _regeneratorRuntime().wrap(function _callee$(_context) {
      while (1) switch (_context.prev = _context.next) {
        case 0:
          if (!(event.data.type === 'applyToExcel')) {
            _context.next = 10;
            break;
          }
          _context.prev = 1;
          parsedData = typeof event.data.data === 'string' ? JSON.parse(event.data.data) : event.data.data;
          _context.next = 5;
          return appendDataToExcel(parsedData);
        case 5:
          _context.next = 10;
          break;
        case 7:
          _context.prev = 7;
          _context.t0 = _context["catch"](1);
          console.error('Error handling applyToExcel:', _context.t0);
        case 10:
        case "end":
          return _context.stop();
      }
    }, _callee, null, [[1, 7]]);
  }));
  return _handleMessage.apply(this, arguments);
}
function appendDataToExcel(_x2) {
  return _appendDataToExcel.apply(this, arguments);
}
function _appendDataToExcel() {
  _appendDataToExcel = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee3(data) {
    return _regeneratorRuntime().wrap(function _callee3$(_context4) {
      while (1) switch (_context4.prev = _context4.next) {
        case 0:
          _context4.prev = 0;
          _context4.next = 3;
          return Excel.run(/*#__PURE__*/function () {
            var _ref = _asyncToGenerator(/*#__PURE__*/_regeneratorRuntime().mark(function _callee2(context) {
              var range, colDefs, rows, headers, values, _iterator, _step, _loop, sheet, startRow, startCol, endRow, endCol, targetRange, headerRange, dataRange, borders, _i, _borders, border;
              return _regeneratorRuntime().wrap(function _callee2$(_context3) {
                while (1) switch (_context3.prev = _context3.next) {
                  case 0:
                    range = context.workbook.getSelectedRange();
                    range.load("address");
                    range.load("rowIndex");
                    range.load("columnIndex");
                    _context3.next = 6;
                    return context.sync();
                  case 6:
                    if (!(data.widget && data.widget.last_step && data.widget.last_step.data)) {
                      _context3.next = 11;
                      break;
                    }
                    colDefs = data.widget.last_step.data.columns;
                    rows = data.widget.last_step.data.rows;
                    _context3.next = 13;
                    break;
                  case 11:
                    console.error('Unexpected data structure:', data);
                    return _context3.abrupt("return");
                  case 13:
                    // Build header row from headerName, look up row values by field
                    headers = colDefs.map(function (col) {
                      return col.headerName || col.field || '';
                    });
                    values = [headers];
                    _iterator = _createForOfIteratorHelper(rows);
                    _context3.prev = 16;
                    _loop = /*#__PURE__*/_regeneratorRuntime().mark(function _loop() {
                      var row;
                      return _regeneratorRuntime().wrap(function _loop$(_context2) {
                        while (1) switch (_context2.prev = _context2.next) {
                          case 0:
                            row = _step.value;
                            values.push(colDefs.map(function (col) {
                              var field = col.field || col.colId || col.headerName;
                              var value = row[field];
                              if (value === undefined || value === null) return '';
                              return _typeof(value) === 'object' ? JSON.stringify(value) : value;
                            }));
                          case 2:
                          case "end":
                            return _context2.stop();
                        }
                      }, _loop);
                    });
                    _iterator.s();
                  case 19:
                    if ((_step = _iterator.n()).done) {
                      _context3.next = 23;
                      break;
                    }
                    return _context3.delegateYield(_loop(), "t0", 21);
                  case 21:
                    _context3.next = 19;
                    break;
                  case 23:
                    _context3.next = 28;
                    break;
                  case 25:
                    _context3.prev = 25;
                    _context3.t1 = _context3["catch"](16);
                    _iterator.e(_context3.t1);
                  case 28:
                    _context3.prev = 28;
                    _iterator.f();
                    return _context3.finish(28);
                  case 31:
                    sheet = range.worksheet;
                    startRow = range.rowIndex;
                    startCol = range.columnIndex;
                    endRow = startRow + values.length - 1;
                    endCol = startCol + headers.length - 1;
                    targetRange = sheet.getRangeByIndexes(startRow, startCol, values.length, headers.length);
                    targetRange.values = values;

                    // Format header row
                    headerRange = sheet.getRangeByIndexes(startRow, startCol, 1, headers.length);
                    headerRange.format.fill.color = "#ADD8E6";
                    headerRange.format.font.bold = true;

                    // Add borders
                    dataRange = sheet.getRangeByIndexes(startRow, startCol, values.length, headers.length);
                    borders = ["EdgeTop", "EdgeBottom", "EdgeLeft", "EdgeRight", "InsideHorizontal", "InsideVertical"];
                    for (_i = 0, _borders = borders; _i < _borders.length; _i++) {
                      border = _borders[_i];
                      dataRange.format.borders.getItem(border).style = "Continuous";
                      dataRange.format.borders.getItem(border).weight = "Thin";
                    }

                    // Auto-fit columns
                    targetRange.getEntireColumn().format.autofitColumns();
                    _context3.next = 47;
                    return context.sync();
                  case 47:
                    console.log('Data appended successfully');
                  case 48:
                  case "end":
                    return _context3.stop();
                }
              }, _callee2, null, [[16, 25, 28, 31]]);
            }));
            return function (_x3) {
              return _ref.apply(this, arguments);
            };
          }());
        case 3:
          _context4.next = 8;
          break;
        case 5:
          _context4.prev = 5;
          _context4.t0 = _context4["catch"](0);
          console.error('Error appending data to Excel:', _context4.t0);
        case 8:
        case "end":
          return _context4.stop();
      }
    }, _callee3, null, [[0, 5]]);
  }));
  return _appendDataToExcel.apply(this, arguments);
}
window.onerror = function (message, source, lineno, colno, error) {
  console.error('Global error:', message, 'at', source, lineno, colno);
  return true;
};
}();
// This entry need to be wrapped in an IIFE because it need to be in strict mode.
!function() {
"use strict";
/*!************************************!*\
  !*** ./src/taskpane/taskpane.html ***!
  \************************************/
__webpack_require__.r(__webpack_exports__);
// Module
var code = "<!DOCTYPE html>\n<html>\n<head>\n    <meta charset=\"UTF-8\" />\n    <meta http-equiv=\"X-UA-Compatible\" content=\"IE=Edge\" />\n    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n    <title>Bag of words - Excel Add-on</title>\n\n    <!-- Office JavaScript API -->\n    <" + "script type=\"text/javascript\" src=\"https://appsforoffice.microsoft.com/lib/1.1/hosted/office.js\"><" + "/script>\n\n</head>\n\n<body>\n    <iframe\n      id=\"webAppFrame\"\n      src=\"https://localhost:3000\"\n      style=\"width: 100%; background-color: #fff; height: 100%; min-height: 100%; height:600px; border: none;\"\n    ></iframe>\n\n    <" + "script>\n        Office.onReady((info) => {\n            if (info.host === Office.HostType.Excel) {\n                const iframe = document.getElementById('webAppFrame');\n                \n                function sendExcelInitializedMessage() {\n                    try {\n                        if (iframe && iframe.contentWindow) {\n                            iframe.contentWindow.postMessage({ type: 'excelInitialized' }, '*');\n                        }\n                    } catch (error) {\n                        console.error('Error sending message to iframe:', error);\n                    }\n                }\n\n                iframe.onload = () => {\n                    setTimeout(sendExcelInitializedMessage, 1000);\n                };\n\n                sendExcelInitializedMessage();\n\n                setInterval(sendExcelInitializedMessage, 5000);\n\n                // Add event listener for cell selection\n                Excel.run(async (context) => {\n                    context.workbook.worksheets.onSelectionChanged.add(handleSelectionChange);\n                    await context.sync();\n                    console.log(\"Selection change event listener added\");\n                    \n                    // Immediately get and send the current selection\n                    await sendCurrentSelection(context);\n                });\n            }\n        });\n\n        async function handleSelectionChange(event) {\n            console.log(\"Selection changed event triggered\");\n            await Excel.run(async (context) => {\n                await sendCurrentSelection(context);\n            });\n        }\n\n        async function sendCurrentSelection(context) {\n            const range = context.workbook.getSelectedRange();\n            range.load(\"address\");\n            const sheet = range.worksheet;\n            sheet.load(\"name\");\n            \n            await context.sync();\n            \n            const sheetData = await getSheetData(context, sheet);\n            \n            // Post message to iframe\n            const iframe = document.getElementById('webAppFrame');\n            const message = {\n                type: 'cellSelected',\n                address: range.address,\n                sheetName: sheet.name,\n                sheetData: sheetData\n            };\n            console.log('Sending message:', message);\n            try {\n                if (iframe && iframe.contentWindow) {\n                    iframe.contentWindow.postMessage(message, '*');\n                }\n            } catch (error) {\n                console.error('Error sending message to iframe:', error);\n            }\n        }\n\n        async function getSheetData(context, sheet) {\n            const usedRange = sheet.getUsedRange();\n            usedRange.load(\"values\");\n            await context.sync();\n            return usedRange.values;\n        }\n    <" + "/script>\n</body>\n\n</html>\n";
// Exports
/* harmony default export */ __webpack_exports__["default"] = (code);
}();
/******/ })()
;
//# sourceMappingURL=taskpane.js.map